// domain-pending(DIV-CHAT-TOOL-001)

import { createHash } from "node:crypto";

import {
  createAgentRunEventSupervisor,
  type AgentRunEventStore,
  type AgentRunEventSupervisor,
} from "./agent-run-events";
import {
  createAgentToolRunner,
  type AgentToolOutcome,
  type AgentToolRegistry,
  type AgentToolScheduler,
  type AgentToolTraceEvent,
} from "./agent-tools";
import type { AgentApprovalCoordinator } from "./agent-approval-coordinator";
import type { ChatGenerationSourceInput } from "./generation-source";
import { chatLog } from "./dev-stack-log";
import {
  gatewayDelta,
  gatewayDeltaContent,
  gatewayDeltaReasoning,
  gatewayFailure,
  gatewayUsage,
  runGatewaySseRequest,
} from "./gateway-sse";

const SAFE_TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$/u;
/** OpenAI `tools[].function.name` charset. Not SAFE_TOKEN: call ids, property
 * names, and stored event tokens legitimately contain `.` `:` `/` `@` `+`. */
const ADVERTISED_FUNCTION_NAME = /^[a-zA-Z0-9_-]{1,64}$/;
const SAFE_TEXT = /^[^\u0000-\u001f\u007f]{1,240}$/u;
const MAX_TOOL_ARGUMENT_BYTES = 16_384;

export interface GatewayReadOnlyToolSchema {
  readonly name: string;
  readonly description: string;
  readonly parameters: {
    readonly type: "object";
    readonly additionalProperties: false;
    readonly properties: Readonly<Record<string, {
      readonly type: "string";
      readonly description?: string;
      readonly enum?: readonly string[];
    }>>;
    readonly required: readonly string[];
  };
}

export interface GatewayReadOnlyToolLoopOptions {
  /** A closed registry whose names must match the advertised tool schemas. */
  readonly registry: AgentToolRegistry;
  /** Single-tool shorthand; use `tools` when advertising more than one. */
  readonly tool?: GatewayReadOnlyToolSchema;
  /** Multi-tool advertisement for the gateway lane. */
  readonly tools?: readonly GatewayReadOnlyToolSchema[];
  /** The same append-only ledger used by the generation supervisor. */
  readonly agentRunEvents: AgentRunEventStore;
  /** Required when any advertised tool is approval-required. */
  readonly approvalCoordinator?: AgentApprovalCoordinator;
  readonly nowEpochMilliseconds: () => number;
  readonly scheduler?: AgentToolScheduler;
}

export interface GatewayToolLoopStartOptions {
  readonly endpoint: string;
  readonly laneId: string;
  readonly serviceToken: string;
  readonly serviceCaller: string;
  readonly usageFeature: string;
  readonly fetch: typeof fetch;
  readonly baseMessages: readonly Readonly<{ readonly role: "system" | "user" | "assistant"; readonly content: string }>[];
  readonly loop: GatewayReadOnlyToolLoopOptions;
  readonly input: ChatGenerationSourceInput;
  readonly fail: (error: unknown) => void;
  readonly complete: () => void;
  readonly isCancelled: () => boolean;
  readonly retrySleep?: (ms: number) => Promise<void>;
}

export interface GatewayToolLoopRun {
  cancel(): void;
}

type ProviderToolCall = {
  readonly id: string;
  readonly name: string;
  readonly argumentsJson: string;
};

const ownPlainObject = (value: unknown): Record<string, unknown> | null => {
  if (value === null || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) return null;
  return value as Record<string, unknown>;
};

const exactKeys = (value: Record<string, unknown>, expected: readonly string[]): boolean => {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
};

const normalizeAdvertisedTools = (
  loop: GatewayReadOnlyToolLoopOptions,
): readonly GatewayReadOnlyToolSchema[] => {
  if (loop.tools !== undefined) {
    if (loop.tool !== undefined) throw new TypeError("configure one gateway tool advertisement");
    if (loop.tools.length === 0) throw new TypeError("invalid gateway tool configuration");
    return loop.tools;
  }
  if (loop.tool !== undefined) return Object.freeze([loop.tool]);
  throw new TypeError("invalid gateway tool configuration");
};

const validateAdvertisedToolSchema = (
  schema: GatewayReadOnlyToolSchema,
  registry: AgentToolRegistry,
): Readonly<{ type: "function"; function: GatewayReadOnlyToolSchema }> => {
  const tool = ownPlainObject(schema);
  const parameters = ownPlainObject(schema.parameters);
  const properties = ownPlainObject(schema.parameters.properties);
  if (tool === null || parameters === null || properties === null
    || !exactKeys(tool, ["description", "name", "parameters"])
    || !exactKeys(parameters, ["additionalProperties", "properties", "required", "type"])
    || !SAFE_TEXT.test(schema.description)
    || schema.parameters.type !== "object" || schema.parameters.additionalProperties !== false
    || !Array.isArray(schema.parameters.required)) {
    throw new TypeError("invalid gateway tool configuration");
  }
  if (!ADVERTISED_FUNCTION_NAME.test(schema.name)) {
    throw new TypeError(
      `invalid advertised tool name ${JSON.stringify(schema.name)}; expected a string matching ^[a-zA-Z0-9_-]{1,64}$`,
    );
  }
  const definition = registry.resolve(schema.name);
  if (definition === null
    || (definition.risk !== "safe" && definition.risk !== "approval-required")) {
    throw new TypeError("invalid gateway tool configuration");
  }
  const required = new Set(schema.parameters.required);
  if (required.size !== schema.parameters.required.length
    || [...required].some((name) => !SAFE_TOKEN.test(name) || !(name in properties))) {
    throw new TypeError("invalid gateway tool configuration");
  }
  for (const [name, rawProperty] of Object.entries(properties)) {
    const property = ownPlainObject(rawProperty);
    if (!SAFE_TOKEN.test(name) || property === null
      || !exactKeys(property, property.description === undefined
        ? (property.enum === undefined ? ["type"] : ["enum", "type"])
        : (property.enum === undefined ? ["description", "type"] : ["description", "enum", "type"]))
      || property.type !== "string"
      || (property.description !== undefined
        && (typeof property.description !== "string" || !SAFE_TEXT.test(property.description)))
      || (property.enum !== undefined && (!Array.isArray(property.enum) || property.enum.length === 0
        || property.enum.some((entry) => typeof entry !== "string" || !SAFE_TOKEN.test(entry))))) {
      throw new TypeError("invalid gateway tool configuration");
    }
  }
  return Object.freeze({ type: "function", function: schema });
};

const validateToolLoop = (
  loop: GatewayReadOnlyToolLoopOptions,
): readonly Readonly<{ type: "function"; function: GatewayReadOnlyToolSchema }>[] => {
  const schemas = normalizeAdvertisedTools(loop);
  const registryNames = [...loop.registry.names()].sort();
  const schemaNames = schemas.map((schema) => schema.name).sort();
  if (registryNames.length !== schemaNames.length
    || registryNames.some((name, index) => name !== schemaNames[index])) {
    throw new TypeError("invalid gateway tool configuration");
  }
  const providerTools = schemas.map((schema) => validateAdvertisedToolSchema(schema, loop.registry));
  const needsCoordinator = schemas.some((schema) =>
    loop.registry.resolve(schema.name)?.risk === "approval-required");
  if (needsCoordinator && loop.approvalCoordinator === undefined) {
    throw new TypeError("approval-required gateway tools require a coordinator");
  }
  return Object.freeze(providerTools);
};

const validatesAgainstToolSchema = (
  schema: GatewayReadOnlyToolSchema,
  input: unknown,
): boolean => {
  const record = ownPlainObject(input);
  if (record === null) return false;
  const propertyNames = Object.keys(schema.parameters.properties);
  if (Object.keys(record).some((name) => !propertyNames.includes(name))) return false;
  if (schema.parameters.required.some((name) => !Object.hasOwn(record, name))) return false;
  return Object.entries(record).every(([name, value]) => {
    const property = schema.parameters.properties[name];
    return property !== undefined && typeof value === "string"
      && (property.enum === undefined || property.enum.includes(value));
  });
};

export const validateGatewayReadOnlyToolLoop = (
  loop: GatewayReadOnlyToolLoopOptions,
): readonly Readonly<{ type: "function"; function: GatewayReadOnlyToolSchema }>[] =>
  validateToolLoop(loop);

const canonicalJson = (value: unknown): string => {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.keys(value as Record<string, unknown>).sort().map((key) =>
    `${JSON.stringify(key)}:${canonicalJson((value as Record<string, unknown>)[key])}`).join(",")}}`;
};

const inputKey = (toolName: string, input: unknown): string =>
  `sha256:${createHash("sha256").update(`${toolName}\n${canonicalJson(input)}`, "utf8").digest("hex")}`;

const stableCallId = (input: ChatGenerationSourceInput): string => {
  const seed = `${input.generationId}\n${input.attemptId ?? input.generationId}`;
  return `toolcall:${createHash("sha256").update(seed, "utf8").digest("hex").slice(0, 32)}`;
};

const appendSyntheticFailure = (
  events: AgentRunEventSupervisor,
  input: ChatGenerationSourceInput,
  callId: string,
  toolName: string,
  idempotencyKey: string,
  code: string,
  summary: string,
): AgentToolOutcome => {
  const attemptId = input.attemptId ?? `${input.generationId}:attempt:unknown`;
  events.toolRequest({ runId: input.generationId, attemptId, callId, toolName,
    timeoutMs: 1, idempotencyKey });
  events.toolError({ runId: input.generationId, attemptId, callId, toolName,
    errorCode: code, errorSummary: summary, retryable: false });
  return Object.freeze({ kind: "failed", callId, code, summary, retryable: false });
};

const priorOutcome = (
  store: AgentRunEventStore,
  runId: string,
  callId: string,
  toolName: string,
  idempotencyKey: string,
): AgentToolOutcome | null => {
  const events = store.list(runId);
  const request = events.find((event) => event.kind === "tool_request" && event.callId === callId);
  if (request === undefined) return null;
  if (request.kind !== "tool_request" || request.toolName !== toolName
    || request.idempotencyKey !== idempotencyKey) {
    return Object.freeze({ kind: "failed", callId, code: "tool_idempotency_conflict",
      summary: "The tool replay conflicts with the durable request.", retryable: false });
  }
  const terminal = events.find((event) =>
    (event.kind === "tool_result" || event.kind === "tool_error") && event.callId === callId);
  if (terminal !== undefined && (terminal.toolName !== request.toolName
    || terminal.attemptId !== request.attemptId)) {
    return Object.freeze({ kind: "failed", callId, code: "tool_idempotency_conflict",
      summary: "The tool replay conflicts with the durable result.", retryable: false });
  }
  if (terminal?.kind === "tool_result") return Object.freeze({
    kind: "completed", callId, summary: terminal.resultSummary,
    durationMs: terminal.durationMs, retryable: terminal.retryable,
  });
  if (terminal?.kind === "tool_error") return Object.freeze({
    kind: "failed", callId, code: terminal.errorCode,
    summary: terminal.errorSummary, retryable: terminal.retryable,
  });
  return Object.freeze({ kind: "failed", callId, code: "tool_in_progress",
    summary: "The durable tool request has no terminal result.", retryable: false });
};

const REASONING_KEYS = Object.freeze(["reasoning_content", "reasoning"] as const);

const captureReasoning = (
  record: Record<string, unknown>,
  into: { reasoning_content?: string; reasoning?: string },
): void => {
  const delta = gatewayDelta(record);
  if (delta === null) return;
  for (const key of REASONING_KEYS) {
    const value = delta[key];
    if (typeof value === "string" && value.length > 0) {
      into[key] = `${into[key] ?? ""}${value}`;
    }
  }
};

const toolHistory = (
  call: ProviderToolCall,
  outcome: AgentToolOutcome,
  reasoning: Readonly<{ reasoning_content?: string; reasoning?: string }> = {},
): readonly Readonly<Record<string, unknown>>[] => {
  const assistant: Record<string, unknown> = {
    role: "assistant",
    content: null,
  };
  for (const key of REASONING_KEYS) {
    const value = reasoning[key];
    if (typeof value === "string" && value.length > 0) assistant[key] = value;
  }
  assistant.tool_calls = [Object.freeze({
    id: call.id,
    type: "function",
    function: Object.freeze({ name: call.name, arguments: call.argumentsJson }),
  })];
  return Object.freeze([
    Object.freeze(assistant),
    Object.freeze({
      role: "tool",
      tool_call_id: call.id,
      content: outcome.kind === "completed" ? outcome.summary
        : outcome.kind === "failed" ? outcome.summary
          : "The tool call did not complete.",
    }),
  ]);
};

export const startGatewayReadOnlyToolLoop = (
  options: GatewayToolLoopStartOptions,
): GatewayToolLoopRun => {
  const providerTools = validateToolLoop(options.loop);
  const advertisedSchemas = new Map(providerTools.map((tool) => [tool.function.name, tool.function]));
  const controller = new AbortController();
  let activeRunner: ReturnType<typeof createAgentToolRunner> | null = null;
  let activeCallId: string | null = null;
  let activeApprovalRunId: string | null = null;

  void (async (): Promise<void> => {
    const input = options.input;
    const attemptId = input.attemptId ?? `${input.generationId}:attempt:unknown`;
    const ledger = createAgentRunEventSupervisor({
      events: options.loop.agentRunEvents,
      nowEpochMilliseconds: options.loop.nowEpochMilliseconds,
      eventId: (runId, sequence, kind) => `${runId}:event:${sequence}:${kind}`,
    });
    let messages: readonly Readonly<Record<string, unknown>>[] = options.baseMessages;
    const failLoop = (
      round: number,
      reason: string,
      error: ReturnType<typeof gatewayFailure> = gatewayFailure("generation_provider_failed"),
    ): void => {
      chatLog("error", "tool_loop_failed", {
        generationId: input.generationId,
        attemptId,
        round,
        reason,
      });
      options.fail(error);
    };
    for (let round = 0; round < 2 && !options.isCancelled(); round += 1) {
      let callId = "";
      let toolName = "";
      let argumentsJson = "";
      let sawToolCallFragment = false;
      let invalidToolCall = false;
      const reasoning: { reasoning_content?: string; reasoning?: string } = {};
      const liveness = { signaledReasoning: false };
      const stream = await runGatewaySseRequest({
        fetch: options.fetch,
        url: options.endpoint,
        init: {
          method: "POST",
          headers: {
            "authorization": `Bearer ${options.serviceToken}`,
            "content-type": "application/json",
            "x-omi-service-caller": options.serviceCaller,
            "x-omi-user-uid": input.context.ownerAccountId,
            "x-omi-llm-feature": options.usageFeature,
          },
          body: JSON.stringify({
            model: options.laneId,
            messages,
            tools: providerTools,
            tool_choice: round === 0 ? "auto" : "none",
            stream: true,
            stream_options: { include_usage: true },
          }),
          signal: controller.signal,
        },
        isCancelled: options.isCancelled,
        onRecord: (record) => {
          if (invalidToolCall) return;
          captureReasoning(record, reasoning);
          if (gatewayDeltaReasoning(record) !== null && !liveness.signaledReasoning) {
            liveness.signaledReasoning = true;
            input.onDelta("");
          }
          const content = gatewayDeltaContent(record);
          if (content !== null) input.onDelta(content);
          const delta = gatewayDelta(record);
          const calls = delta?.tool_calls;
          if (calls !== undefined) {
            sawToolCallFragment = true;
            // Round 1 asked for no tools. Do not execute a stray call, and do
            // not set invalidToolCall: that would drop content already on the
            // wire. The post-stream path completes if content arrived.
            if (round !== 0) return;
            if (!Array.isArray(calls) || calls.length !== 1) {
              invalidToolCall = true;
              return;
            }
            const fragment = ownPlainObject(calls[0]);
            const fn = ownPlainObject(fragment?.function);
            if (fragment === null || fn === null
              || (fragment.index !== undefined && fragment.index !== 0)
              || (fragment.id !== undefined && typeof fragment.id !== "string")
              || (fn.name !== undefined && typeof fn.name !== "string")
              || (fn.arguments !== undefined && typeof fn.arguments !== "string")) {
              invalidToolCall = true;
              return;
            }
            if (typeof fragment.id === "string") callId += fragment.id;
            if (typeof fn.name === "string") toolName += fn.name;
            if (typeof fn.arguments === "string") argumentsJson += fn.arguments;
            if (argumentsJson.length > MAX_TOOL_ARGUMENT_BYTES) invalidToolCall = true;
          }
          const usage = gatewayUsage(record, `${attemptId}:usage:${round + 1}`);
          if (usage !== null) {
            chatLog("info", "usage", {
              generationId: input.generationId,
              attemptId,
              inputTokens: usage.inputTokens,
              outputTokens: usage.outputTokens,
              totalTokens: usage.totalTokens,
            });
            input.onUsage?.(usage);
          }
        },
        generationId: input.generationId,
        attemptId,
        sleep: options.retrySleep,
        retryEmptyDone: true,
        // OpenCode zen/go (deepseek-v4-flash) appends a valid-JSON trailer
        // after [DONE] (`{"choices":[],"cost":"0"}` or `event: ping`). The
        // gateway observer counts those frames and still succeeds; without
        // this flag the service reader classifies the same 200 as invalid.
        // Post-DONE records are not applied (no extra content, no extra
        // tool_calls). Same flag as the non-tool generation path.
        allowDataAfterDone: true,
      });
      if (options.isCancelled() || stream.kind === "cancelled") return;
      if (stream.kind === "failed") {
        failLoop(round, "gateway_stream_failed", stream.error);
        return;
      }
      if (invalidToolCall) {
        failLoop(round, "invalid_tool_call");
        return;
      }
      // Unreachable while retryEmptyDone is true: that retry converts a
      // 200/[DONE] with no content and no tool calls into
      // gateway_stream_failed before this branch. Kept so turning the retry
      // off still names the invariant.
      if (!stream.stats.sawDone && !stream.stats.sawContent && !sawToolCallFragment) {
        failLoop(round, "empty_stream");
        return;
      }
      if (!stream.stats.sawDone) {
        chatLog("warn", "stream_ended_without_done", {
          generationId: input.generationId,
          attemptId,
          attempt: stream.attempt,
          sawContent: stream.stats.sawContent,
        });
      }
      if (round !== 0) {
        if (stream.stats.sawContent) {
          if (sawToolCallFragment) {
            chatLog("warn", "tool_loop_ignored", {
              generationId: input.generationId,
              attemptId,
              round,
              reason: "tool_call_after_tool_round",
            });
          }
          options.complete();
          return;
        }
        // empty_content here is also unreachable while retryEmptyDone is
        // true: no content and no tool calls already failed as
        // gateway_stream_failed. A stray tool call with no content uses
        // tool_call_after_tool_round instead.
        failLoop(round, sawToolCallFragment ? "tool_call_after_tool_round" : "empty_content");
        return;
      }
      if (sawToolCallFragment && callId.length === 0 && toolName.length === 0 && argumentsJson.length === 0) {
        failLoop(round, "empty_tool_call_fragments");
        return;
      }
      if (callId.length === 0 && toolName.length === 0 && argumentsJson.length === 0) {
        // Unreachable while retryEmptyDone is true: no content and no tool
        // calls already failed as gateway_stream_failed after empty_done
        // retries. Round-0 tool fragments with empty tokens take the branch
        // above instead.
        if (!stream.stats.sawContent) {
          failLoop(round, "empty_content");
          return;
        }
        options.complete();
        return;
      }
      if (!SAFE_TOKEN.test(callId) || !SAFE_TOKEN.test(toolName)) {
        failLoop(round, "unsafe_tool_call_tokens");
        return;
      }
      const canonicalCallId = stableCallId(input);
      activeCallId = canonicalCallId;
      let parsedInput: unknown;
      let outcome: AgentToolOutcome;
      try {
        parsedInput = JSON.parse(argumentsJson);
      } catch {
        const idem = inputKey(toolName, { malformed: true });
        try {
          outcome = appendSyntheticFailure(ledger, input, canonicalCallId, toolName, idem,
            "tool_invalid_input", "The tool request is invalid.");
        } catch {
          failLoop(round, "tool_ledger_failed");
          return;
        }
        messages = Object.freeze([...messages, ...toolHistory(
          { id: callId, name: toolName, argumentsJson }, outcome, reasoning,
        )]);
        continue;
      }
      const idem = inputKey(toolName, parsedInput);
      const replay = priorOutcome(options.loop.agentRunEvents, input.generationId,
        canonicalCallId, toolName, idem);
      if (replay !== null) {
        outcome = replay;
      } else if (options.loop.registry.resolve(toolName) === null) {
        try {
          outcome = appendSyntheticFailure(ledger, input, canonicalCallId, toolName, idem,
            "tool_unknown", "The requested tool is unavailable.");
        } catch {
          failLoop(round, "tool_ledger_failed");
          return;
        }
      } else {
        const definition = options.loop.registry.resolve(toolName)!;
        const advertised = advertisedSchemas.get(toolName);
        if (advertised === undefined) {
          try {
            outcome = appendSyntheticFailure(ledger, input, canonicalCallId, toolName, idem,
              "tool_unknown", "The requested tool is unavailable.");
          } catch {
            failLoop(round, "tool_ledger_failed");
            return;
          }
        } else if (!validatesAgainstToolSchema(advertised, parsedInput)) {
          try {
            outcome = appendSyntheticFailure(ledger, input, canonicalCallId, toolName, idem,
              "tool_invalid_input", "The tool request is invalid.");
          } catch {
            failLoop(round, "tool_ledger_failed");
            return;
          }
        } else if (definition.risk === "approval-required") {
          const coordinator = options.loop.approvalCoordinator;
          if (coordinator === undefined) {
            failLoop(round, "tool_approval_missing");
            return;
          }
          activeApprovalRunId = input.generationId;
          outcome = await coordinator.request({
            runId: input.generationId,
            attemptId,
            call: {
              callId: canonicalCallId,
              toolName,
              idempotencyKey: idem,
              input: parsedInput,
            },
          });
          if (options.isCancelled()) return;
          if (outcome.kind === "pending_approval") {
            outcome = await coordinator.waitForResolution({
              runId: input.generationId,
              approvalId: outcome.approvalId,
              callId: canonicalCallId,
              isCancelled: options.isCancelled,
            });
          }
          activeApprovalRunId = null;
          if (options.isCancelled()) return;
          if (outcome.kind === "cancelled") {
            failLoop(round, "tool_approval_cancelled");
            return;
          }
        } else {
          try {
            ledger.toolRequest({
              runId: input.generationId, attemptId, callId: canonicalCallId,
              toolName, timeoutMs: definition.timeoutMs, idempotencyKey: idem,
            });
          } catch {
            failLoop(round, "tool_ledger_failed");
            return;
          }
          let ledgerError = false;
          let terminalRecorded = false;
          const runner = createAgentToolRunner({
            registry: options.loop.registry,
            nowEpochMilliseconds: options.loop.nowEpochMilliseconds,
            scheduler: options.loop.scheduler,
            onEvent: (event: AgentToolTraceEvent): void => {
              try {
                if (event.kind === "tool_result") {
                  ledger.toolResult({
                    runId: input.generationId, attemptId, callId: event.callId,
                    toolName: event.toolName, resultSummary: event.summary,
                    durationMs: event.durationMs, retryable: event.retryable,
                  });
                  terminalRecorded = true;
                }
                if (event.kind === "tool_error") {
                  ledger.toolError({
                    runId: input.generationId, attemptId, callId: event.callId,
                    toolName: event.toolName, errorCode: event.code,
                    errorSummary: event.summary, retryable: event.retryable,
                  });
                  terminalRecorded = true;
                }
              } catch { ledgerError = true; }
            },
          });
          activeRunner = runner;
          outcome = await runner.request({
            callId: canonicalCallId,
            toolName,
            idempotencyKey: idem,
            input: parsedInput,
          });
          activeRunner = null;
          if (options.isCancelled()) return;
          if (!ledgerError && !terminalRecorded && outcome.kind === "failed") {
            try {
              ledger.toolError({
                runId: input.generationId, attemptId, callId: outcome.callId,
                toolName, errorCode: outcome.code, errorSummary: outcome.summary,
                retryable: outcome.retryable,
              });
              terminalRecorded = true;
            } catch { ledgerError = true; }
          }
          if (ledgerError || outcome.kind === "pending_approval" || outcome.kind === "cancelled") {
            failLoop(round, ledgerError ? "tool_ledger_failed" : "tool_approval_cancelled");
            return;
          }
        }
      }
      messages = Object.freeze([...messages, ...toolHistory(
        { id: callId, name: toolName, argumentsJson }, outcome, reasoning,
      )]);
    }
    if (!options.isCancelled()) failLoop(2, "tool_loop_exhausted");
  })();

  return Object.freeze({
    cancel(): void {
      controller.abort();
      if (activeApprovalRunId !== null && options.loop.approvalCoordinator !== undefined) {
        void options.loop.approvalCoordinator.cancelPending(activeApprovalRunId);
        activeApprovalRunId = null;
      }
      if (activeRunner !== null && activeCallId !== null) activeRunner.cancel(activeCallId);
    },
  });
};

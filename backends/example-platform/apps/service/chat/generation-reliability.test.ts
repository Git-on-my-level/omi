import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createAgentRunEventSupervisor, createInMemoryAgentRunEventStore } from "./agent-run-events";
import { createAgentToolRegistry, type AgentToolDefinition } from "./agent-tools";
import { logLineHasSecret } from "./dev-stack-log";
import { createChatGenerationContextPacket } from "./generation-context";
import {
  createGatewayChatGenerationSource,
  type ChatGenerationSourceInput,
} from "./generation-source";

const SECRET_TOKEN = "service-secret-do-not-log";
const PROMPT = "What should I do next?";

const context = createChatGenerationContextPacket({
  accountId: "account-1",
  generationId: "generation-1",
  nowEpochMilliseconds: 1,
  candidates: [{
    sourceKind: "memory",
    sourceId: "source-1",
    claimId: null,
    evidenceId: "evidence-1",
    ownerAccountId: "account-1",
    sourceHash: `sha256:${"1".repeat(64)}`,
    capturedAt: 1,
    expiresAt: null,
    redactedPreview: "The user prefers concise answers.",
    tokenEstimate: 6,
    inclusionReason: "authorized context",
  }],
});

const input = (
  overrides: Partial<ChatGenerationSourceInput> = {},
): ChatGenerationSourceInput => ({
  generationId: "generation-1",
  attemptId: "generation-1:attempt:1",
  prompt: PROMPT,
  context,
  attachments: [],
  onDelta() {},
  onComplete() {},
  onError() {},
  ...overrides,
});

const encodeSse = (payload: string): Uint8Array =>
  new TextEncoder().encode(`data: ${payload}\n\n`);

const streamFromChunks = (...chunks: readonly Uint8Array[]): Response => new Response(
  new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  }),
  { status: 200, headers: { "content-type": "text/event-stream" } },
);

const sse = (...events: readonly string[]): Response =>
  streamFromChunks(...events.map((event) => encodeSse(event)));

const delayedPreamble = (delayMs: number): Response => new Response(
  new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encodeSse(JSON.stringify({
        choices: [{ delta: { reasoning_content: "thinking" } }],
      })));
      await Bun.sleep(delayMs);
      controller.enqueue(encodeSse(JSON.stringify({
        choices: [{ delta: { content: "ok" } }],
      })));
      controller.enqueue(encodeSse("[DONE]"));
      controller.close();
    },
  }),
  { status: 200, headers: { "content-type": "text/event-stream" } },
);

const statusResponse = (status: number): Response => new Response("no", { status });

const readOnlyToolSchema = Object.freeze({
  name: "safe_fixture_status",
  description: "Read the current fixture status.",
  parameters: Object.freeze({
    type: "object" as const,
    additionalProperties: false as const,
    properties: Object.freeze({
      scope: Object.freeze({ type: "string" as const, enum: Object.freeze(["current"]) }),
    }),
    required: Object.freeze(["scope"]),
  }),
});

const safeReadTool = (execute: AgentToolDefinition["execute"]): AgentToolDefinition => ({
  schemaVersion: 1,
  name: "safe_fixture_status",
  risk: "safe",
  timeoutMs: 100,
  retryable: false,
  displaySummary: "Read fixture status",
  validateInput: (value): boolean => value !== null && typeof value === "object"
    && !Array.isArray(value) && Object.keys(value).length === 1
    && (value as Record<string, unknown>).scope === "current",
  execute,
});

const seedAgentLedger = () => {
  const store = createInMemoryAgentRunEventStore();
  createAgentRunEventSupervisor({ events: store, nowEpochMilliseconds: () => 1 })
    .accepted({ runId: "generation-1", attemptId: "generation-1:attempt:1", admissionId: "admission-1" });
  return store;
};

const runSource = async (
  fetchImpl: typeof fetch,
  extra: Partial<Parameters<typeof createGatewayChatGenerationSource>[0]> = {},
): Promise<{ text: string; error: unknown; usage: unknown[] }> => {
  const source = createGatewayChatGenerationSource({
    gatewayUrl: "http://127.0.0.1:8787",
    laneId: "omi:auto:chat-agent",
    serviceToken: SECRET_TOKEN,
    retrySleep: async () => {},
    fetch: fetchImpl,
    ...extra,
  });
  const usage: unknown[] = [];
  return await new Promise((resolve) => {
    const text: string[] = [];
    source.start(input({
      onDelta: (delta) => { if (delta.length > 0) text.push(delta); },
      onUsage: (entry) => usage.push(entry),
      onComplete: () => resolve({ text: text.join(""), error: null, usage }),
      onError: (error) => resolve({ text: text.join(""), error, usage }),
    }));
  });
};

const readChatLog = (runDir: string): readonly Record<string, unknown>[] => {
  const path = join(runDir, "logs", "chat.jsonl");
  return readFileSync(path, "utf8").trim().split("\n").map((line) => JSON.parse(line) as Record<string, unknown>);
};

describe("chat generation reliability", () => {
  const originalRunDir = process.env.OMI_DEV_STACK_RUNDIR;
  let runDir = "";

  const isolateLogs = (): string => {
    runDir = mkdtempSync(join(tmpdir(), "omi-chat-reliability-"));
    process.env.OMI_DEV_STACK_RUNDIR = runDir;
    return runDir;
  };

  afterEach(() => {
    if (originalRunDir === undefined) delete process.env.OMI_DEV_STACK_RUNDIR;
    else process.env.OMI_DEV_STACK_RUNDIR = originalRunDir;
    if (runDir.length > 0) rmSync(runDir, { recursive: true, force: true });
    runDir = "";
  });

  test("slow reasoning preamble is measured and still completes", async () => {
    isolateLogs();
    const result = await runSource(async () => delayedPreamble(40));
    expect(result.error).toBeNull();
    expect(result.text).toBe("ok");
    const events = readChatLog(runDir).map((row) => row.event);
    expect(events).toContain("generation_admitted");
    expect(events).toContain("provider_request_started");
    expect(events).toContain("reasoning_preamble");
    expect(events).toContain("first_content_delta");
    expect(events).toContain("generation_terminal");
    const firstContent = readChatLog(runDir).find((row) => row.event === "first_content_delta");
    expect(firstContent?.msSinceStart).toBeGreaterThanOrEqual(40);
    expect(firstContent?.reasoningPreambleMs).toBeGreaterThanOrEqual(40);
  });

  test("retries a 429 once, then completes, and logs both attempts", async () => {
    isolateLogs();
    let calls = 0;
    const result = await runSource(async () => {
      calls += 1;
      if (calls === 1) return statusResponse(429);
      return sse(
        JSON.stringify({ choices: [{ delta: { content: "recovered" } }] }),
        "[DONE]",
      );
    });
    expect(calls).toBe(2);
    expect(result.error).toBeNull();
    expect(result.text).toBe("recovered");
    const started = readChatLog(runDir).filter((row) => row.event === "provider_request_started");
    expect(started).toHaveLength(2);
    expect(started.map((row) => row.attempt)).toEqual([1, 2]);
    expect(readChatLog(runDir).some((row) => row.event === "provider_attempt_failed" && row.reason === "http_429")).toBe(true);
  });

  test("exhausts retries on repeated 429 and surfaces generation_rate_limited", async () => {
    isolateLogs();
    let calls = 0;
    const result = await runSource(async () => {
      calls += 1;
      return statusResponse(429);
    });
    expect(calls).toBe(3);
    expect(result.error).toEqual({ code: "generation_rate_limited", retryable: true });
    const started = readChatLog(runDir).filter((row) => row.event === "provider_request_started");
    expect(started).toHaveLength(3);
    expect(readChatLog(runDir).filter((row) => row.event === "provider_attempt_failed" && row.reason === "http_429")).toHaveLength(3);
    expect(readChatLog(runDir).at(-1)).toMatchObject({ event: "generation_terminal", outcome: "failed" });
  });

  test("exhausts retries on repeated 5xx and logs every attempt", async () => {
    isolateLogs();
    let calls = 0;
    const result = await runSource(async () => {
      calls += 1;
      return statusResponse(503);
    });
    expect(calls).toBe(3);
    expect(result.error).toEqual({ code: "generation_provider_failed", retryable: true });
    const started = readChatLog(runDir).filter((row) => row.event === "provider_request_started");
    expect(started).toHaveLength(3);
    expect(readChatLog(runDir).filter((row) => row.event === "provider_attempt_failed")).toHaveLength(3);
    expect(readChatLog(runDir).at(-1)).toMatchObject({ event: "generation_terminal", outcome: "failed" });
  });

  test("does not retry a 4xx that is about the request", async () => {
    isolateLogs();
    let calls = 0;
    const result = await runSource(async () => {
      calls += 1;
      return statusResponse(400);
    });
    expect(calls).toBe(1);
    expect(result.error).toEqual({ code: "generation_provider_failed", retryable: true });
  });

  test("does not retry after content has been delivered", async () => {
    isolateLogs();
    let calls = 0;
    const result = await runSource(async () => {
      calls += 1;
      return new Response(new ReadableStream<Uint8Array>({
        async start(controller) {
          controller.enqueue(encodeSse(JSON.stringify({ choices: [{ delta: { content: "partial" } }] })));
          await new Promise((resolve) => setTimeout(resolve, 0));
          controller.error(new Error("reset after content"));
        },
      }), { status: 200 });
    });
    expect(calls).toBe(1);
    expect(result.text).toBe("partial");
    expect(result.error).toEqual({ code: "generation_provider_failed", retryable: true });
  });

  test("a stream that ends without [DONE] after content still completes", async () => {
    isolateLogs();
    const result = await runSource(async () => sse(
      JSON.stringify({ choices: [{ delta: { content: "done-less" } }] }),
    ));
    expect(result.error).toBeNull();
    expect(result.text).toBe("done-less");
    expect(readChatLog(runDir).some((row) => row.event === "stream_ended_without_done")).toBe(true);
  });

  test("SSE frames split across chunk boundaries still parse", async () => {
    isolateLogs();
    const payload = `data: ${JSON.stringify({ choices: [{ delta: { content: "split" } }] })}\n\ndata: [DONE]\n\n`;
    const bytes = new TextEncoder().encode(payload);
    const result = await runSource(async () => streamFromChunks(bytes.slice(0, 17), bytes.slice(17)));
    expect(result.error).toBeNull();
    expect(result.text).toBe("split");
  });

  test("a leftover [DONE] without a trailing blank line still completes", async () => {
    isolateLogs();
    const result = await runSource(async () => new Response(
      `data: ${JSON.stringify({ choices: [{ delta: { content: "tail" } }] })}\n\ndata: [DONE]`,
      { status: 200, headers: { "content-type": "text/event-stream" } },
    ));
    expect(result.error).toBeNull();
    expect(result.text).toBe("tail");
  });

  test("tool-loop retries empty [DONE] then completes when content arrives", async () => {
    isolateLogs();
    const store = seedAgentLedger();
    const responses = [
      sse(JSON.stringify({ choices: [{ delta: { reasoning_content: "thinking" } }] }), "[DONE]"),
      sse(
        JSON.stringify({ choices: [{ delta: { content: "ok" } }] }),
        "[DONE]",
      ),
    ];
    const result = await runSource(async () => responses.shift()!, {
      readOnlyToolLoop: {
        registry: createAgentToolRegistry([safeReadTool(async () => ({
          summary: "Fixture is ready.", durationMs: 2, retryable: false,
        }))]),
        tool: readOnlyToolSchema,
        agentRunEvents: store,
        nowEpochMilliseconds: () => 2,
      },
    });
    expect(result.error).toBeNull();
    expect(result.text).toBe("ok");
    expect(readChatLog(runDir).some((row) => row.event === "provider_attempt_failed" && row.reason === "empty_done")).toBe(true);
    expect(readChatLog(runDir).filter((row) => row.event === "provider_request_started")).toHaveLength(2);
  });

  test("tool-call round trip logs the required events and carries no secrets", async () => {
    isolateLogs();
    const store = seedAgentLedger();
    const responses = [
      sse(JSON.stringify({
        choices: [{ delta: { tool_calls: [{ index: 0, id: "provider-call",
          function: { name: "safe_fixture_status", arguments: "{\"scope\":\"current\"}" } }] } }],
      }), "[DONE]"),
      sse(
        JSON.stringify({ choices: [{ delta: { content: "Canonical answer." } }] }),
        JSON.stringify({ usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 } }),
        "[DONE]",
      ),
    ];
    const result = await runSource(async () => responses.shift()!, {
      readOnlyToolLoop: {
        registry: createAgentToolRegistry([safeReadTool(async () => ({
          summary: "Fixture is ready.", durationMs: 2, retryable: false,
        }))]),
        tool: readOnlyToolSchema,
        agentRunEvents: store,
        nowEpochMilliseconds: () => 2,
      },
    });
    expect(result.error).toBeNull();
    expect(result.text).toBe("Canonical answer.");
    const raw = readFileSync(join(runDir, "logs", "chat.jsonl"), "utf8");
    expect(logLineHasSecret(raw, [SECRET_TOKEN, PROMPT, "Bearer", "provider-call", "scope"])).toBe(false);
    const events = raw.trim().split("\n").map((line) => JSON.parse(line) as Record<string, unknown>);
    expect(events.some((row) => row.event === "generation_admitted")).toBe(true);
    expect(events.some((row) => row.event === "provider_request_started")).toBe(true);
    expect(events.some((row) => row.event === "first_content_delta")).toBe(true);
    expect(events.some((row) => row.event === "usage")).toBe(true);
    expect(events.at(-1)).toMatchObject({ event: "generation_terminal", outcome: "done" });
  });

  test("retries an empty [DONE] stream once, then completes when content arrives", async () => {
    isolateLogs();
    let calls = 0;
    const result = await runSource(async () => {
      calls += 1;
      if (calls === 1) {
        return sse(
          JSON.stringify({ choices: [{ delta: { reasoning_content: "thinking" } }] }),
          "[DONE]",
        );
      }
      return sse(
        JSON.stringify({ choices: [{ delta: { content: "ok" } }] }),
        "[DONE]",
      );
    });
    expect(calls).toBe(2);
    expect(result.error).toBeNull();
    expect(result.text).toBe("ok");
    expect(readChatLog(runDir).some((row) => row.event === "provider_attempt_failed" && row.reason === "empty_done")).toBe(true);
  });

  test("exhausts retries when every attempt is [DONE] with no content", async () => {
    isolateLogs();
    let calls = 0;
    const result = await runSource(async () => {
      calls += 1;
      return sse("[DONE]");
    });
    expect(calls).toBe(3);
    expect(result.error).toEqual({ code: "generation_provider_failed", retryable: true });
    expect(readChatLog(runDir).filter((row) => row.event === "provider_attempt_failed" && row.reason === "empty_done")).toHaveLength(3);
  });

  test("reliability log lines never carry secrets", async () => {
    isolateLogs();
    await runSource(async () => sse(
      JSON.stringify({ choices: [{ delta: { content: "safe" } }] }),
      "[DONE]",
    ));
    const raw = readFileSync(join(runDir, "logs", "chat.jsonl"), "utf8");
    expect(logLineHasSecret(raw, [SECRET_TOKEN, PROMPT, "Bearer ", "sk-"])).toBe(false);
    for (const row of readChatLog(runDir)) {
      expect(row.proc).toBe("chat");
      expect(typeof row.ts).toBe("string");
      expect(typeof row.level).toBe("string");
      expect(typeof row.event).toBe("string");
    }
  });

  const toolLoop = (): Partial<Parameters<typeof createGatewayChatGenerationSource>[0]> => ({
    readOnlyToolLoop: {
      registry: createAgentToolRegistry([safeReadTool(async () => ({
        summary: "Fixture is ready.", durationMs: 2, retryable: false,
      }))]),
      tool: readOnlyToolSchema,
      agentRunEvents: seedAgentLedger(),
      nowEpochMilliseconds: () => 2,
    },
  });

  const toolCallDelta = (
    id = "provider-call",
    name = "safe_fixture_status",
    argumentsJson = "{\"scope\":\"current\"}",
  ) => JSON.stringify({
    choices: [{ delta: { tool_calls: [{ index: 0, id, function: { name, arguments: argumentsJson } }] } }],
  });

  test("round-1 replay missing reasoning_content completes once the assistant turn is replayed", async () => {
    // red-proof: dropping reasoning_content from the round-1 assistant turn
    // makes this stub 400, the source calls onError, and the streamed answer
    // never completes. The live DeepSeek probe returned that 400 verbatim.
    isolateLogs();
    const reasoning = "private-reasoning-not-for-client";
    const answer = "Harborline is open today.";
    const bodies: Record<string, unknown>[] = [];
    const result = await runSource(async (_url, init) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      bodies.push(body);
      const messages = Array.isArray(body.messages) ? body.messages as Record<string, unknown>[] : [];
      if (!messages.some((message) => message.role === "tool")) {
        return sse(
          JSON.stringify({ choices: [{ delta: { reasoning_content: reasoning } }] }),
          toolCallDelta(),
          "[DONE]",
        );
      }
      const assistant = messages.find((message) => message.role === "assistant");
      if (typeof assistant?.reasoning_content !== "string" || assistant.reasoning_content.length === 0) {
        return new Response(JSON.stringify({
          error: {
            type: "invalid_request_error",
            code: "invalid_request_error",
            message: "The `reasoning_content` in the thinking mode must be passed back to the API.",
          },
        }), { status: 400 });
      }
      return sse(JSON.stringify({ choices: [{ delta: { content: answer } }] }), "[DONE]");
    }, toolLoop());
    expect(result.error).toBeNull();
    expect(result.text).toBe(answer);
    const replay = bodies[1]?.messages as Record<string, unknown>[] | undefined;
    const assistant = replay?.find((message) => message.role === "assistant");
    expect(assistant?.reasoning_content).toBe(reasoning);
    expect(result.text).not.toContain(reasoning);
    const raw = readFileSync(join(runDir, "logs", "chat.jsonl"), "utf8");
    expect(raw).not.toContain(reasoning);
    expect(readChatLog(runDir).at(-1)).toMatchObject({ event: "generation_terminal", outcome: "done" });
  });

  test("a stray round-1 tool call does not replace content already delivered", async () => {
    // red-proof: invalidToolCall on round !== 0 fails the generation after
    // onDelta has already forwarded the answer, which Chat then replaces with
    // "The chat provider is unavailable."
    isolateLogs();
    const answer = "Let me check your current action items for you.";
    let executions = 0;
    const result = await runSource(async (_url, init) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      const messages = Array.isArray(body.messages) ? body.messages as Record<string, unknown>[] : [];
      if (!messages.some((message) => message.role === "tool")) {
        return sse(toolCallDelta(), "[DONE]");
      }
      return sse(
        JSON.stringify({ choices: [{ delta: { content: answer } }] }),
        toolCallDelta("stray-call"),
        "[DONE]",
      );
    }, {
      readOnlyToolLoop: {
        registry: createAgentToolRegistry([safeReadTool(async () => {
          executions += 1;
          return { summary: "Fixture is ready.", durationMs: 2, retryable: false };
        })]),
        tool: readOnlyToolSchema,
        agentRunEvents: seedAgentLedger(),
        nowEpochMilliseconds: () => 2,
      },
    });
    expect(result.error).toBeNull();
    expect(result.text).toBe(answer);
    expect(executions).toBe(1);
    expect(readChatLog(runDir).some((row) =>
      row.event === "tool_loop_ignored" && row.reason === "tool_call_after_tool_round")).toBe(true);
    expect(readChatLog(runDir).at(-1)).toMatchObject({ event: "generation_terminal", outcome: "done" });
  });

  test("each tool-loop generation_provider_failed branch logs a distinct reason", async () => {
    isolateLogs();
    const runFailing = async (fetchImpl: typeof fetch) => {
      const result = await runSource(fetchImpl, toolLoop());
      expect(result.error).toEqual({ code: "generation_provider_failed", retryable: true });
    };

    await runFailing(async () => new Response("no", { status: 400 }));

    await runFailing(async () => sse(
      JSON.stringify({
        choices: [{ delta: { tool_calls: [
          { index: 0, id: "a", function: { name: "safe_fixture_status", arguments: "{}" } },
          { index: 1, id: "b", function: { name: "safe_fixture_status", arguments: "{}" } },
        ] } }],
      }),
      "[DONE]",
    ));

    await runFailing(async () => sse(
      JSON.stringify({ choices: [{ delta: { tool_calls: [{ index: 0, function: {} }] } }] }),
      "[DONE]",
    ));

    await runFailing(async () => sse(toolCallDelta("-bad"), "[DONE]"));

    const round1ToolCallNoContent: Response[] = [
      sse(toolCallDelta(), "[DONE]"),
      sse(toolCallDelta("stray-call"), "[DONE]"),
    ];
    await runFailing(async () => round1ToolCallNoContent.shift()!);

    const reasons = readChatLog(runDir)
      .filter((row) => row.event === "tool_loop_failed")
      .map((row) => row.reason);
    // empty_stream and empty_content cannot fire while retryEmptyDone is
    // true: a 200/[DONE] with no content and no tool calls is retried and
    // then logged as gateway_stream_failed before the loop sees those
    // branches. Do not list them here as if they were reachable.
    expect(reasons).toEqual([
      "gateway_stream_failed",
      "invalid_tool_call",
      "empty_tool_call_fragments",
      "unsafe_tool_call_tokens",
      "tool_call_after_tool_round",
    ]);
    expect(reasons).not.toContain("empty_stream");
    expect(reasons).not.toContain("empty_content");
    const raw = readFileSync(join(runDir, "logs", "chat.jsonl"), "utf8");
    expect(logLineHasSecret(raw, [SECRET_TOKEN, PROMPT, "Bearer ", "private-reasoning"])).toBe(false);
  });

  const deepseekStopBody = (answer: string): readonly string[] => Object.freeze([
    JSON.stringify({
      id: "chatcmpl-ds",
      object: "chat.completion.chunk",
      choices: [{
        index: 0,
        delta: { role: "assistant", content: null, reasoning_content: "The user asked to say hi." },
        finish_reason: null,
      }],
      usage: null,
    }),
    JSON.stringify({
      choices: [{
        index: 0,
        delta: { content: null, reasoning_content: " Three words." },
        finish_reason: null,
      }],
      usage: null,
    }),
    JSON.stringify({
      choices: [{ index: 0, delta: { content: answer }, finish_reason: null }],
      usage: null,
    }),
    JSON.stringify({
      choices: [{ index: 0, delta: { content: null }, finish_reason: "stop" }],
      usage: null,
    }),
    JSON.stringify({
      choices: [],
      usage: { prompt_tokens: 965, completion_tokens: 19, total_tokens: 984 },
    }),
    "[DONE]",
    JSON.stringify({ choices: [], cost: "0" }),
  ]);

  const deepseekToolCallBody = (): readonly string[] => Object.freeze([
    JSON.stringify({
      choices: [{
        index: 0,
        delta: { role: "assistant", content: null, reasoning_content: "I should check today's actions." },
        finish_reason: null,
      }],
      usage: null,
    }),
    toolCallDelta(),
    JSON.stringify({
      choices: [{ index: 0, delta: { content: null }, finish_reason: "tool_calls" }],
      usage: null,
    }),
    JSON.stringify({
      choices: [],
      usage: { prompt_tokens: 800, completion_tokens: 40, total_tokens: 840 },
    }),
    "[DONE]",
    JSON.stringify({ choices: [], cost: "0" }),
  ]);

  test("OpenCode cost trailer after [DONE] does not fail a DeepSeek stop stream", async () => {
    // red-proof: live round 0 on deepseek-v4-flash through zen/go was a 200
    // with sawDone and finishReason=stop, then the service logged
    // tool_loop_failed reason=gateway_stream_failed. The gateway observer
    // accepted the post-DONE `{"choices":[],"cost":"0"}` trailer; the
    // service reader did not. Stub replays that shape. No provider.
    isolateLogs();
    const answer = "Hey there!";
    const result = await runSource(async () => sse(...deepseekStopBody(answer)), toolLoop());
    expect(result.error).toBeNull();
    expect(result.text).toBe(answer);
    expect(result.usage).toEqual([expect.objectContaining({
      inputTokens: 965,
      outputTokens: 19,
      totalTokens: 984,
    })]);
    const events = readChatLog(runDir).map((row) => row.event);
    expect(events).not.toContain("tool_loop_failed");
    expect(readChatLog(runDir).at(-1)).toMatchObject({ event: "generation_terminal", outcome: "done" });
    const raw = readFileSync(join(runDir, "logs", "chat.jsonl"), "utf8");
    expect(raw).not.toContain("The user asked to say hi.");
    expect(logLineHasSecret(raw, [SECRET_TOKEN, PROMPT, "Bearer "])).toBe(false);
  });

  test("OpenCode cost trailer after [DONE] does not fail a DeepSeek tool_calls stream", async () => {
    isolateLogs();
    const answer = "You met Maya at Harborline.";
    const result = await runSource(async (_url, init) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      const messages = Array.isArray(body.messages) ? body.messages as Record<string, unknown>[] : [];
      if (!messages.some((message) => message.role === "tool")) {
        return sse(...deepseekToolCallBody());
      }
      return sse(...deepseekStopBody(answer));
    }, toolLoop());
    expect(result.error).toBeNull();
    expect(result.text).toBe(answer);
    expect(readChatLog(runDir).some((row) => row.event === "tool_loop_failed")).toBe(false);
    expect(readChatLog(runDir).at(-1)).toMatchObject({ event: "generation_terminal", outcome: "done" });
  });

  test("empty [DONE] is gateway_stream_failed, never empty_stream or empty_content", async () => {
    isolateLogs();
    const result = await runSource(async () => sse("[DONE]"), toolLoop());
    expect(result.error).toEqual({ code: "generation_provider_failed", retryable: true });
    const reasons = readChatLog(runDir)
      .filter((row) => row.event === "tool_loop_failed")
      .map((row) => row.reason);
    expect(reasons).toEqual(["gateway_stream_failed"]);
    expect(readChatLog(runDir).filter((row) => row.event === "provider_attempt_failed")
      .every((row) => row.reason === "empty_done")).toBe(true);
  });
});

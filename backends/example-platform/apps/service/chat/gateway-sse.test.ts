import { describe, expect, test } from "bun:test";

import { consumeGatewaySse } from "./gateway-sse";

/**
 * Observed OpenCode zen/go trailer after a DeepSeek thinking-mode stream:
 * a 200, `[DONE]`, then a valid-JSON event the gateway observer still
 * accepts (`{"choices":[],"cost":"0"}`). Live chat.jsonl for
 * "say hi in three words" logged usage (965/19/984) and then
 * `tool_loop_failed reason=gateway_stream_failed` on round 0 while the
 * gateway recorded `sawDone=true finishReason=stop`.
 *
 * Do not invent a different trailer. This is the shape quoted from
 * opencode-go and matching that log pair.
 */
const opencodeDeepseekStopBody = (answer: string): string => [
  `data: ${JSON.stringify({
    id: "chatcmpl-ds",
    object: "chat.completion.chunk",
    choices: [{
      index: 0,
      delta: { role: "assistant", content: null, reasoning_content: "The user asked to say hi." },
      finish_reason: null,
    }],
    usage: null,
  })}\n\n`,
  `data: ${JSON.stringify({
    choices: [{ index: 0, delta: { content: null, reasoning_content: " Three words." }, finish_reason: null }],
    usage: null,
  })}\n\n`,
  `data: ${JSON.stringify({
    choices: [{ index: 0, delta: { content: answer }, finish_reason: null }],
    usage: null,
  })}\n\n`,
  `data: ${JSON.stringify({
    choices: [{ index: 0, delta: { content: null }, finish_reason: "stop" }],
    usage: null,
  })}\n\n`,
  `data: ${JSON.stringify({
    choices: [],
    usage: { prompt_tokens: 965, completion_tokens: 19, total_tokens: 984 },
  })}\n\n`,
  "data: [DONE]\n\n",
  `data: ${JSON.stringify({ choices: [], cost: "0" })}\n\n`,
].join("");

const bodyStream = (text: string): ReadableStream<Uint8Array> =>
  new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      controller.close();
    },
  });

const sseDataPayloads = (event: string): readonly string[] => Object.freeze(event
  .split(/\r?\n/u)
  .filter((line) => line.startsWith("data:"))
  .map((line) => line.slice(5).trimStart()));

/**
 * Replica of `observeUpstreamSse` dispatch/note in
 * `integration/local-model-gateway.mjs`. The proxy tees bytes; this only
 * recreates the observer that decided the live DeepSeek stream was fine.
 */
const observeUpstreamSseReplica = async (
  body: ReadableStream<Uint8Array>,
  startedAt: number,
): Promise<{
  readonly sawDone: boolean;
  readonly frameCount: number;
  readonly firstContentMs: number | null;
  readonly firstReasoningMs: number | null;
  readonly finishReason: string | null;
}> => {
  let frameCount = 0;
  let buffer = "";
  let sawDone = false;
  let firstContentMs: number | null = null;
  let firstReasoningMs: number | null = null;
  let finishReason: string | null = null;
  const decoder = new TextDecoder();
  const note = (record: Record<string, unknown>): void => {
    const choices = Array.isArray(record.choices) ? record.choices[0] : null;
    if (choices === null || typeof choices !== "object") return;
    const elapsed = Date.now() - startedAt;
    const reason = (choices as { finish_reason?: unknown }).finish_reason;
    if (typeof reason === "string" && /^[a-z_]{1,32}$/u.test(reason)) finishReason = reason;
    const delta = (choices as { delta?: unknown }).delta;
    if (delta === null || typeof delta !== "object") return;
    const deltaRecord = delta as Record<string, unknown>;
    const reasoning = typeof deltaRecord.reasoning_content === "string"
      && deltaRecord.reasoning_content.length > 0
      ? deltaRecord.reasoning_content
      : (typeof deltaRecord.reasoning === "string" && deltaRecord.reasoning.length > 0
        ? deltaRecord.reasoning
        : null);
    if (reasoning !== null && firstReasoningMs === null) firstReasoningMs = elapsed;
    if (typeof deltaRecord.content === "string" && deltaRecord.content.length > 0
      && firstContentMs === null) {
      firstContentMs = elapsed;
    }
  };
  const dispatch = (event: string): void => {
    const data = sseDataPayloads(event).join("\n");
    if (data.length === 0) return;
    frameCount += 1;
    if (data === "[DONE]") {
      sawDone = true;
      return;
    }
    try {
      const parsed = JSON.parse(data) as unknown;
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
        note(parsed as Record<string, unknown>);
      }
    } catch {
      // Count the frame; never inspect payload text beyond the JSON shape.
    }
  };
  const reader = body.getReader();
  while (true) {
    const next = await reader.read();
    if (next.value !== undefined) {
      buffer += decoder.decode(next.value, { stream: !next.done });
      const events = buffer.split(/\r?\n\r?\n/u);
      buffer = events.pop() ?? "";
      for (const event of events) dispatch(event);
    }
    if (next.done) {
      if (buffer.trim().length > 0) dispatch(buffer);
      break;
    }
  }
  return { sawDone, frameCount, firstContentMs, firstReasoningMs, finishReason };
};

describe("gateway SSE parsers on observed DeepSeek/OpenCode frames", () => {
  test("cost trailer after [DONE]: gateway observer accepts, service reader rejects unless allowDataAfterDone", async () => {
    const body = opencodeDeepseekStopBody("Hey there!");
    const gateway = await observeUpstreamSseReplica(bodyStream(body), Date.now());
    expect(gateway.sawDone).toBe(true);
    expect(gateway.finishReason).toBe("stop");
    expect(gateway.firstReasoningMs).not.toBeNull();
    expect(gateway.firstContentMs).not.toBeNull();
    expect(gateway.frameCount).toBeGreaterThan(0);

    const applied: Record<string, unknown>[] = [];
    const serviceDefault = await consumeGatewaySse({
      body: bodyStream(body),
      isCancelled: () => false,
      onRecord: (record) => { applied.push(record); },
      startedAt: Date.now(),
    });
    expect(serviceDefault.kind).toBe("invalid");

    const appliedAllowed: Record<string, unknown>[] = [];
    const serviceAllowed = await consumeGatewaySse({
      body: bodyStream(body),
      isCancelled: () => false,
      onRecord: (record) => { appliedAllowed.push(record); },
      startedAt: Date.now(),
      allowDataAfterDone: true,
    });
    expect(serviceAllowed.kind).toBe("records");
    if (serviceAllowed.kind === "records") {
      expect(serviceAllowed.stats.sawDone).toBe(true);
      expect(serviceAllowed.stats.sawContent).toBe(true);
      expect(serviceAllowed.stats.sawReasoning).toBe(true);
    }
    expect(appliedAllowed.some((record) => record.cost === "0")).toBe(false);
    expect(JSON.stringify(appliedAllowed)).toContain("Hey there!");
    expect(JSON.stringify(applied)).not.toContain("cost");
  });

  test("ping event after [DONE] is the same parser split", async () => {
    const body = `${opencodeDeepseekStopBody("Hey there!")}event: ping\ndata: {}\n\n`;
    const gateway = await observeUpstreamSseReplica(bodyStream(body), Date.now());
    expect(gateway.sawDone).toBe(true);
    expect(gateway.finishReason).toBe("stop");

    const denied = await consumeGatewaySse({
      body: bodyStream(body),
      isCancelled: () => false,
      onRecord() {},
      startedAt: Date.now(),
    });
    expect(denied.kind).toBe("invalid");

    const allowed = await consumeGatewaySse({
      body: bodyStream(body),
      isCancelled: () => false,
      onRecord() {},
      startedAt: Date.now(),
      allowDataAfterDone: true,
    });
    expect(allowed.kind).toBe("records");
  });
});

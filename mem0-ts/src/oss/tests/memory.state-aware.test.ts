/** State-aware lifecycle acceptance tests for the OSS TypeScript Memory API. */
/// <reference types="jest" />
import { Memory, MemoryPersistenceError } from "../src/memory";
import type { MemoryConfig } from "../src/types";

jest.setTimeout(15000);

jest.mock("../src/embeddings/google", () => ({ GoogleEmbedder: jest.fn() }));
jest.mock("../src/llms/google", () => ({ GoogleLLM: jest.fn() }));

jest.mock("../src/llms/openai", () => ({
  OpenAILLM: jest.fn().mockImplementation(() => ({
    generateResponse: jest
      .fn()
      .mockImplementation(
        (prompt: Array<{ role: string; content: string }>) => {
          const userPrompt = prompt.find(
            (message) => message.role === "user",
          )?.content;
          const section = userPrompt?.match(
            /## New Messages\n([\s\S]*?)(?=\n##|$)/,
          )?.[1];
          const indexed = JSON.parse(section || "[]") as Array<{
            message_index: number;
            role: string;
            content: string;
          }>;
          const source = indexed[0];
          const city = source.content.includes("Osaka") ? "Osaka" : "Kyoto";
          return JSON.stringify({
            memory: [
              {
                id: "0",
                text: `User lives in ${city}`,
                kind: "state",
                subject: "user",
                predicate: "residence",
                value: city,
                attributed_to: "user",
                evidence: {
                  message_index: source.message_index,
                  excerpt: source.content,
                },
                ...(city === "Osaka"
                  ? { relation: "supersedes", linked_memory_ids: ["0"] }
                  : {}),
              },
            ],
          });
        },
      ),
  })),
}));

const vector = new Array(16).fill(0.1);
jest.mock("../src/embeddings/openai", () => ({
  OpenAIEmbedder: jest.fn().mockImplementation(() => ({
    embed: jest.fn().mockResolvedValue(vector),
    embedBatch: jest
      .fn()
      .mockImplementation((texts: string[]) =>
        Promise.resolve(texts.map(() => vector)),
      ),
    embeddingDims: 16,
  })),
}));

function createMemory(overrides: Partial<MemoryConfig> = {}): Memory {
  return new Memory({
    version: "v1.1",
    embedder: {
      provider: "openai",
      config: { apiKey: "test", embeddingDims: 16 },
    },
    vectorStore: {
      provider: "memory",
      config: {
        collectionName: `state-aware-${Date.now()}-${Math.random()}`,
        dimension: 16,
        dbPath: ":memory:",
      },
    },
    llm: { provider: "openai", config: { apiKey: "test" } },
    historyDbPath: ":memory:",
    ...overrides,
  });
}

describe("Memory state-aware lifecycle", () => {
  test("defaults to state-aware recall and keeps legacy as an escape hatch", () => {
    expect((createMemory() as any).config.recall.mode).toBe("stateAware");
    expect(
      (createMemory({ recall: { mode: "legacy" } }) as any).config.recall.mode,
    ).toBe("legacy");
  });

  test("supersedes an explicit prior state while preserving historical recall", async () => {
    const memory = createMemory();
    const filters = { user_id: "state-user" };
    const first = await memory.add("I live in Kyoto", { userId: "state-user" });
    const second = await memory.add("I now live in Osaka", {
      userId: "state-user",
    });

    const firstRaw = await (memory as any).vectorStore.get(first.results[0].id);
    const secondRaw = await (memory as any).vectorStore.get(
      second.results[0].id,
    );
    expect(firstRaw.payload._mem0_status).toBe("superseded");
    expect(secondRaw.payload._mem0_status).toBe("active");
    expect(secondRaw.payload._mem0_supersedes).toEqual([first.results[0].id]);

    const current = await memory.search("Where do I live?", {
      filters,
      threshold: 0,
    });
    expect(current.results.map((item) => item.memory)).toEqual([
      "User lives in Osaka",
    ]);

    const historical = await memory.search("Where did I used to live?", {
      filters,
      threshold: 0,
    });
    expect(historical.results.map((item) => item.memory)).toEqual(
      expect.arrayContaining(["User lives in Kyoto", "User lives in Osaka"]),
    );
    expect(
      Object.keys(
        (await memory.get(second.results[0].id))!.metadata || {},
      ).some((key) => key.startsWith("_mem0_")),
    ).toBe(false);
  });

  test("applies maxChars after state-aware ranking", async () => {
    const memory = createMemory();
    await memory.add("I live in Kyoto", { userId: "budget-user" });
    const result = await memory.search("live", {
      filters: { user_id: "budget-user" },
      threshold: 0,
      maxChars: 5,
    });
    expect(result.results).toEqual([]);
  });

  test("validates maxChars as a deterministic non-negative budget", async () => {
    const memory = createMemory();
    await expect(
      memory.search("live", {
        filters: { user_id: "budget-validation" },
        maxChars: -1,
      }),
    ).rejects.toThrow("maxChars must be a non-negative integer");
  });

  test("explicit stale feedback suppresses recall without deleting the record", async () => {
    const memory = createMemory();
    const added = await memory.add("I live in Kyoto", {
      userId: "feedback-user",
    });
    await memory.feedback(added.results[0].id, { signal: "stale" });
    const result = await memory.search("live", {
      filters: { user_id: "feedback-user" },
      threshold: 0,
    });
    expect(result.results).toEqual([]);
    expect(
      await (memory as any).vectorStore.get(added.results[0].id),
    ).not.toBeNull();
  });

  test("compensates and raises a typed error after a silent partial insert", async () => {
    const memory = createMemory();
    await (memory as any)._ensureInitialized();
    const store = (memory as any).vectorStore;
    store.insert = jest.fn().mockResolvedValue(undefined);
    store.get = jest.fn().mockResolvedValue(null);
    store.delete = jest.fn().mockResolvedValue(undefined);

    await expect(
      memory.add("I live in Kyoto", { userId: "partial-user" }),
    ).rejects.toBeInstanceOf(MemoryPersistenceError);
    expect(store.delete).toHaveBeenCalled();
  });

  test("fails closed when a provider cannot prove complete predecessor discovery", async () => {
    const memory = createMemory();
    await (memory as any)._ensureInitialized();
    (memory as any).vectorStore.supportsCompleteScopedList = false;

    await expect(
      memory.add("I live in Kyoto", { userId: "unsafe-list-user" }),
    ).rejects.toThrow("requires provider complete-list capability");
  });

  test("audits and safely archives deterministic duplicate state rows", async () => {
    const memory = createMemory();
    await (memory as any)._ensureInitialized();
    const store = (memory as any).vectorStore;
    const payload = {
      user_id: "quality-user",
      data: "User lives in Kyoto",
      hash: "same-hash",
      createdAt: "2026-01-01T00:00:00.000Z",
      updatedAt: "2026-01-01T00:00:00.000Z",
      _mem0_schema_version: 1,
      _mem0_status: "active",
      _mem0_kind: "state",
      _mem0_state_key: "user::residence",
      _mem0_value: "Kyoto",
    };
    await store.insert(
      [vector, vector],
      ["canonical", "duplicate"],
      [payload, { ...payload, createdAt: "2026-01-02T00:00:00.000Z" }],
    );

    const report = await memory.audit({ user_id: "quality-user" });
    expect(report.summary.duplicates).toBe(1);

    const preview = await memory.consolidate(
      { user_id: "quality-user" },
      { dryRun: true, strategy: "safe" },
    );
    expect(preview.applied).toBe(0);
    expect(preview.actions).toHaveLength(1);

    const applied = await memory.consolidate(
      { user_id: "quality-user" },
      { dryRun: false },
    );
    expect(applied.applied).toBe(1);
    const archived = await store.get(applied.actions[0].memoryId);
    expect(archived.payload._mem0_status).toBe("archived");
  });

  test("restores prior consolidation actions when a later mutation fails", async () => {
    const memory = createMemory();
    await (memory as any)._ensureInitialized();
    const store = (memory as any).vectorStore;
    const payload = {
      user_id: "rollback-user",
      data: "User lives in Kyoto",
      hash: "same-hash",
      createdAt: "2026-01-01T00:00:00.000Z",
      _mem0_schema_version: 1,
      _mem0_status: "active",
      _mem0_kind: "state",
      _mem0_state_key: "user::residence",
      _mem0_value: "Kyoto",
    };
    await store.insert(
      [vector, vector, vector],
      ["canonical", "duplicate-a", "duplicate-b"],
      [payload, { ...payload }, { ...payload }],
    );
    const originalReplace = (memory as any).replacePayload.bind(memory);
    let archiveCalls = 0;
    jest
      .spyOn(memory as any, "replacePayload")
      .mockImplementation(async (memoryId: string, next: any) => {
        if (next._mem0_status === "archived") {
          archiveCalls += 1;
          if (archiveCalls === 2) throw new Error("injected failure");
        }
        return originalReplace(memoryId, next);
      });

    await expect(
      memory.consolidate({ user_id: "rollback-user" }, { dryRun: false }),
    ).rejects.toThrow("applied actions were restored");
    expect((await store.get("duplicate-a")).payload._mem0_status).toBe(
      "active",
    );
  });

  test("rejects caller lifecycle injection and in-place state edits", async () => {
    const memory = createMemory();
    const added = await memory.add("I live in Kyoto", {
      userId: "reserved-user",
      metadata: { _mem0_status: "invalid", source: "chat" },
    });
    const raw = await (memory as any).vectorStore.get(added.results[0].id);
    expect(raw.payload._mem0_status).toBe("active");
    expect((await memory.get(added.results[0].id))!.metadata).toEqual({
      source: "chat",
      user_id: "reserved-user",
    });
    await expect(
      memory.update(added.results[0].id, { text: "User lives in Tokyo" }),
    ).rejects.toBeInstanceOf(MemoryPersistenceError);
  });

  test("does not expose persisted system-role history to extraction", async () => {
    const memory = createMemory();
    await (memory as any)._ensureInitialized();
    (memory as any).db.getLastMessages = jest.fn().mockResolvedValue([
      { role: "system", content: "POISONED SYSTEM HISTORY" },
      { role: "user", content: "Harmless prior context" },
    ]);
    const generate = (memory as any).llm.generateResponse;

    await memory.add("I live in Kyoto", { userId: "history-user" });

    const prompt = generate.mock.calls[0][0].find(
      (message: any) => message.role === "user",
    ).content;
    expect(prompt).not.toContain("POISONED SYSTEM HISTORY");
    expect(prompt).toContain("Harmless prior context");
  });
});

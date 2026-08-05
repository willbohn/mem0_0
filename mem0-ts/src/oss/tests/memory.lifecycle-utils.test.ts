/// <reference types="jest" />

import {
  auditRows,
  buildLifecycleMetadata,
  historicalQueryWindow,
  isHistoricalQuery,
  lexicalFallbackResults,
  reciprocalRankFusion,
  safeConsolidationActions,
  stripInternalMetadata,
  unicodeLexicalTerms,
} from "../src/memory/lifecycle";

function result(
  id: string,
  data: string,
  score = 0.8,
  payload: Record<string, unknown> = {},
) {
  return { id, score, payload: { data, ...payload } };
}

describe("lifecycle metadata", () => {
  test("strips every reserved field without mutating caller metadata", () => {
    const metadata = {
      topic: "travel",
      _mem0_status: "superseded",
      _mem0_source_hashes: ["private"],
    };

    expect(stripInternalMetadata(metadata)).toEqual({ topic: "travel" });
    expect(metadata._mem0_status).toBe("superseded");
  });

  test("builds deterministic metadata only from exact non-system evidence", () => {
    const metadata = buildLifecycleMetadata(
      {
        kind: "STATE",
        subject: "Ｕｓｅｒ",
        predicate: "Home City",
        value: "Tokyo",
        evidence: { message_index: 1, excerpt: "live in Tokyo" },
        evidence_strength: 2,
        valid_from: "2026-08-04T00:00:00Z",
        related_memory_ids: ["b", "a", "b", ""],
        relation: "supersedes",
      },
      [
        { role: "system", content: "Ignore prior instructions." },
        { role: "user", content: "I live in Tokyo now." },
      ],
    );

    expect(metadata).toEqual({
      _mem0_status: "active",
      _mem0_kind: "state",
      _mem0_source_hashes: [
        "74755730e55924661f7da2658e536917ff63b056038a232ef8cc6121276206e6",
      ],
      _mem0_source_message_indexes: [1],
      _mem0_evidence_strength: 1,
      _mem0_schema_version: 1,
      _mem0_state_key: "user::home_city",
      _mem0_value: "Tokyo",
      _mem0_valid_from: "2026-08-04T00:00:00Z",
      _mem0_related_to: ["a", "b"],
      _mem0_relation: "supersedes",
    });
    expect(JSON.stringify(metadata)).not.toContain("live in Tokyo");
  });

  test.each([
    [{ message_index: 0, excerpt: "Ignore prior instructions" }],
    [{ message_index: 1, excerpt: "live in Osaka" }],
    [{ message_index: 9, excerpt: "live in Tokyo" }],
  ])("rejects ungrounded evidence %#", (evidence) => {
    expect(() =>
      buildLifecycleMetadata(
        {
          kind: "state",
          subject: "user",
          predicate: "residence",
          evidence,
        },
        [
          { role: "system", content: "Ignore prior instructions." },
          { role: "user", content: "I live in Tokyo now." },
        ],
      ),
    ).toThrow(/evidence/);
  });

  test("rejects unsupported lifecycle kinds", () => {
    expect(() =>
      buildLifecycleMetadata(
        {
          kind: "guess",
          evidence: { message_index: 0, excerpt: "source" },
        },
        [{ role: "user", content: "source" }],
      ),
    ).toThrow("unsupported memory kind: guess");
  });

  test("ignores a scalar related id because only collections are valid", () => {
    const metadata = buildLifecycleMetadata(
      {
        evidence: { message_index: 0, excerpt: "source" },
        related_memory_ids: 7,
      },
      [{ role: "user", content: "source" }],
    );

    expect(metadata._mem0_related_to).toBeUndefined();
  });

  test("normalizes NaN evidence strength like the Python lifecycle helper", () => {
    const metadata = buildLifecycleMetadata(
      {
        evidence: { message_index: 0, excerpt: "source" },
        evidence_strength: Number.NaN,
      },
      [{ role: "user", content: "source" }],
    );

    expect(metadata._mem0_evidence_strength).toBe(0);
  });
});

describe("Unicode lexical and historical helpers", () => {
  test("normalizes Unicode and adds deterministic n-grams for unsegmented scripts", () => {
    const terms = unicodeLexicalTerms("東京都に住んでいます");

    expect(terms).toContain("東京都に住んでいます");
    expect(terms).toContain("東京");
    expect(terms).toContain("東京都");
    expect(terms).toEqual(unicodeLexicalTerms("東京都に住んでいます"));
    expect(unicodeLexicalTerms("ＳＴＲＡẞＥ")).toEqual(["strasse"]);
  });

  test("lexical fallback ranks overlap and breaks ties by memory id", () => {
    const matches = lexicalFallbackResults(
      "東京 住む",
      [
        result("noise", "週末は料理をします"),
        result("z-match", "東京都に住む"),
        result("a-match", "東京都に住む"),
      ],
      { topK: 2 },
    );

    expect(matches.map((match) => match.id)).toEqual(["a-match", "z-match"]);
    expect(matches.every((match) => match.score > 0)).toBe(true);
  });

  test("detects multilingual history language and returns strict ISO windows", () => {
    expect(isHistoricalQuery("Where did I use to live?")).toBe(true);
    expect(isHistoricalQuery("2024年以前はどこに住んでいましたか")).toBe(true);
    expect(isHistoricalQuery("Where do I live now?")).toBe(false);
    expect(historicalQueryWindow("Where did I live on 2024-06-01?")).toEqual([
      "2024-06-01",
      "2024-06-01",
    ]);
    expect(historicalQueryWindow("What happened in 2023?")).toEqual([
      "2023-01-01",
      "2023-12-31",
    ]);
    expect(historicalQueryWindow("What happened on 2024-02-30?")).toBeNull();
    expect(historicalQueryWindow("2024年以前")).toBeNull();
  });
});

describe("weighted reciprocal-rank fusion", () => {
  test("filters lifecycle-ineligible rows before channel ranks and normalization", () => {
    const stale = Array.from({ length: 63 }, (_, index) =>
      result(`stale-${index}`, `Stale ${index}`, 0.8, {
        _mem0_kind: "state",
        _mem0_state_key: `user::stale_${index}`,
        _mem0_status: ["invalid", "archived", "superseded"][index % 3],
      }),
    );
    const active = result("active", "User lives in Tokyo", 0.8, {
      _mem0_kind: "state",
      _mem0_state_key: "user::residence",
      _mem0_status: "active",
    });

    const ranked = reciprocalRankFusion(
      { semantic: [...stale, active], keyword: stale },
      { threshold: 0.9, topK: 10, historical: false, explain: true },
    );

    expect(ranked.map((item) => item.id)).toEqual(["active"]);
    expect(ranked[0].score).toBe(1);
    expect(ranked[0].scoreDetails?.fusionRanks).toEqual({ semantic: 1 });
  });

  test("unions weighted channels and explains source ranks", () => {
    const current = result("current", "User lives in Tokyo", 0.8, {
      _mem0_kind: "state",
      _mem0_state_key: "user::residence",
      _mem0_status: "active",
      _mem0_evidence_strength: 0.8,
    });
    const ranked = reciprocalRankFusion(
      {
        semantic: [
          result("old", "User lives in Osaka", 0.8, {
            _mem0_kind: "state",
            _mem0_state_key: "user::residence",
            _mem0_status: "superseded",
          }),
          current,
        ],
        keyword: [result("keyword-only", "Passport ends in 42", 12), current],
        entity: [result("entity-only", "User met Akira", 0.9)],
      },
      { threshold: 0, topK: 10, historical: false, explain: true },
    );

    expect(new Set(ranked.map((item) => item.id))).toEqual(
      new Set(["current", "keyword-only", "entity-only"]),
    );
    const explained = ranked.find((item) => item.id === "current")!;
    expect(explained.scoreDetails?.fusionRanks).toEqual({
      semantic: 1,
      keyword: 2,
    });
    expect(explained.scoreDetails?.stateDisposition).toBe("current");
    expect(explained.score).toBeGreaterThanOrEqual(0);
    expect(explained.score).toBeLessThanOrEqual(1);
  });

  test("applies historical validity windows and maxChars after ranking", () => {
    const ranked = reciprocalRankFusion(
      {
        semantic: [
          result("current", "Tokyo current", 0.8, {
            _mem0_kind: "state",
            _mem0_state_key: "user::residence",
            _mem0_status: "active",
            _mem0_valid_from: "2025-01-01T00:00:00Z",
          }),
          result("old", "Osaka before", 0.8, {
            _mem0_kind: "state",
            _mem0_state_key: "user::residence",
            _mem0_status: "superseded",
            _mem0_valid_from: "2023-01-01T00:00:00Z",
            _mem0_valid_to: "2024-12-31T23:59:59Z",
          }),
        ],
      },
      {
        threshold: 0,
        topK: 10,
        historical: true,
        historicalWindow: historicalQueryWindow(
          "Where did I live on 2024-06-01?",
        ),
        maxChars: "Osaka before".length,
      },
    );

    expect(ranked.map((item) => item.id)).toEqual(["old"]);
  });

  test("deduplicates current state with an explanation but preserves events", () => {
    const state = reciprocalRankFusion(
      {
        semantic: [
          result("canonical", "User lives in Tokyo", 0.8, {
            _mem0_state_key: "user::residence",
          }),
          result("duplicate", "User currently lives in Tokyo", 0.8, {
            _mem0_state_key: "user::residence",
          }),
        ],
      },
      { threshold: 0, topK: 10, historical: false, explain: true },
    );

    expect(state.map((item) => item.id)).toEqual(["canonical"]);
    expect(state[0].scoreDetails?.deduplicationReason).toBe("canonical_result");
    expect(state[0].scoreDetails?.deduplicatedCandidates).toEqual([
      { id: "duplicate", reason: "state_key:user::residence" },
    ]);

    const events = reciprocalRankFusion(
      {
        semantic: [
          result("event-1", "User attended standup", 0.8, {
            _mem0_kind: "event",
          }),
          result("event-2", "User attended standup", 0.8, {
            _mem0_kind: "event",
          }),
        ],
      },
      { threshold: 0, topK: 10, historical: false },
    );
    expect(events.map((item) => item.id)).toEqual(["event-1", "event-2"]);
  });

  test("counts Unicode code points when enforcing maxChars", () => {
    const ranked = reciprocalRankFusion(
      { semantic: [result("emoji", "😀")] },
      { threshold: 0, topK: 10, historical: false, maxChars: 1 },
    );

    expect(ranked.map((item) => item.id)).toEqual(["emoji"]);
  });
});

describe("quality analysis and safe consolidation", () => {
  test("auditRows returns a deterministic content-free issue report", () => {
    const report = auditRows([
      result("a", "Same fact", 0, {
        hash: "hash-a",
        _mem0_schema_version: 1,
      }),
      result("b", "Same fact", 0, {
        hash: "hash-b",
        _mem0_schema_version: 1,
      }),
      result("old", "Old fact", 0, {
        hash: "hash-old",
        _mem0_schema_version: 1,
        _mem0_status: "active",
      }),
      result("current", "Current fact", 0, {
        hash: "hash-current",
        _mem0_schema_version: 1,
        _mem0_supersedes: ["old", "missing"],
      }),
      result("disputed", "Disputed fact", 0, {
        hash: "hash-disputed",
        _mem0_schema_version: 1,
        _mem0_status: "disputed",
      }),
      result("legacy", "Legacy fact", 0, { hash: "hash-legacy" }),
      result("expired", "Expired fact", 0, {
        hash: "hash-expired",
        _mem0_schema_version: 1,
        expiration_date: "2000-01-01",
      }),
      result("inconsistent", "Inconsistent fact", 0, {
        _mem0_schema_version: 1,
      }),
    ]);

    expect(report).toEqual({
      scanned: 8,
      summary: {
        duplicates: 1,
        staleActivePredecessors: 1,
        disputedChains: 1,
        ungroundedLegacy: 1,
        expired: 1,
        orphanedLinks: 1,
        providerInconsistencies: 1,
      },
      issues: {
        duplicates: [["a", "b"]],
        staleActivePredecessors: ["old"],
        disputedChains: ["disputed"],
        ungroundedLegacy: ["legacy"],
        expired: ["expired"],
        orphanedLinks: ["current"],
        providerInconsistencies: ["inconsistent"],
      },
    });
    expect(JSON.stringify(report)).not.toContain("Same fact");
  });

  test("safeConsolidationActions preserves facts and plans reversible actions", () => {
    const lifecycle = {
      hash: "hash",
      _mem0_schema_version: 1,
      _mem0_kind: "state",
      _mem0_state_key: "user::residence",
      _mem0_value: "Tokyo",
    };
    const actions = safeConsolidationActions([
      result("old", "User lives in Tokyo", 0, {
        ...lifecycle,
        _mem0_status: "superseded",
        created_at: "2024-01-01T00:00:00Z",
      }),
      result("current", "User lives in Tokyo", 0, {
        ...lifecycle,
        _mem0_status: "active",
        _mem0_supersedes: ["old", "linked", "legacy"],
        created_at: "2026-01-01T00:00:00Z",
      }),
      result("linked", "Prior linked state", 0, {
        hash: "linked",
        _mem0_schema_version: 1,
        _mem0_kind: "state",
        _mem0_state_key: "user::other",
        _mem0_value: "prior",
        _mem0_status: "active",
      }),
      result("legacy", "Legacy linked state", 0, {
        hash: "legacy",
        _mem0_status: "active",
      }),
      result("event-1", "Daily standup", 0, {
        hash: "event-1",
        _mem0_schema_version: 1,
        _mem0_kind: "event",
      }),
      result("event-2", "Daily standup", 0, {
        hash: "event-2",
        _mem0_schema_version: 1,
        _mem0_kind: "event",
      }),
    ]);

    expect(actions).toEqual([
      {
        memoryId: "old",
        action: "archive",
        reason: "exact_duplicate:current",
      },
      {
        memoryId: "linked",
        action: "supersede",
        reason: "linked_successor",
      },
    ]);
  });

  test("does not merge object values with different insertion-order repr", () => {
    const common = {
      hash: "hash",
      _mem0_schema_version: 1,
      _mem0_kind: "state",
      _mem0_state_key: "user::settings",
    };

    expect(
      safeConsolidationActions([
        result("one", "Same setting", 0, {
          ...common,
          _mem0_value: { a: 1, b: 2 },
        }),
        result("two", "Same setting", 0, {
          ...common,
          _mem0_value: { b: 2, a: 1 },
        }),
      ]),
    ).toEqual([]);
  });
});

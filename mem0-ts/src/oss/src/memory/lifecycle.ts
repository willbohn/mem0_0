import { createHash } from "node:crypto";

export const internalPrefix = "_mem0_";
export const schemaVersion = 1;
export const memoryKinds: ReadonlySet<string> = new Set([
  "state",
  "preference",
  "event",
  "plan",
  "relationship",
  "instruction",
  "procedure",
]);
export const stateLikeKinds: ReadonlySet<string> = new Set(
  [...memoryKinds].filter((kind) => kind !== "event"),
);
export const suppressedStatuses: ReadonlySet<string> = new Set([
  "invalid",
  "archived",
]);

export type RrfChannel = "semantic" | "keyword" | "entity";

export const rrfWeights: Readonly<Record<RrfChannel, number>> = {
  semantic: 1,
  keyword: 1,
  entity: 0.5,
};
export const rrfConstant = 60;

export type LifecyclePayload = Record<string, unknown>;
export type LifecycleExtraction = Record<string, unknown>;
export interface LifecycleMetadata extends Record<string, unknown> {
  _mem0_related_to?: string[];
}

export interface LifecycleMessage {
  role?: unknown;
  content?: unknown;
}

export interface LifecycleResult {
  id?: unknown;
  score?: unknown;
  payload?: unknown;
}

export type HistoricalWindow = readonly [string, string];
export type RrfChannels = Partial<
  Record<RrfChannel, Iterable<LifecycleResult> | null>
>;

export interface RrfOptions {
  threshold: number;
  topK: number;
  historical: boolean;
  historicalWindow?: HistoricalWindow | null;
  maxChars?: number | null;
  explain?: boolean;
}

export interface LifecycleScoreDetails {
  fusionRanks: Partial<Record<RrfChannel, number>>;
  fusionSourceScores: Partial<Record<RrfChannel, number>>;
  stateDisposition: "current" | "historical" | "disputed";
  deduplicationReason: string | null;
  deduplicatedCandidates?: Array<{ id: string; reason: string }>;
  evidenceStrengthFactor: number;
  finalScore: number;
  threshold: number;
}

export interface FusedLifecycleResult {
  id: string;
  score: number;
  payload: LifecyclePayload;
  scoreDetails?: LifecycleScoreDetails;
}

export interface AuditIssues {
  duplicates: string[][];
  staleActivePredecessors: string[];
  disputedChains: string[];
  ungroundedLegacy: string[];
  expired: string[];
  orphanedLinks: string[];
  providerInconsistencies: string[];
}

export interface AuditReport {
  scanned: number;
  summary: { [K in keyof AuditIssues]: number };
  issues: AuditIssues;
}

export interface ConsolidationAction {
  memoryId: string;
  action: "archive" | "supersede";
  reason: string;
}

interface ResultParts {
  id: string | null;
  score: number;
  payload: LifecyclePayload;
}

interface FusionCandidate {
  id: string;
  payload: LifecyclePayload;
  fusion: number;
  ranks: Partial<Record<RrfChannel, number>>;
  sourceScores: Partial<Record<RrfChannel, number>>;
  score: number;
  evidenceTiebreak: number;
}

const historicalPatterns = [
  /(?<![\p{L}\p{N}_])use[ds]?\s+to(?![\p{L}\p{N}_])/u,
  /(?<![\p{L}\p{N}_])before(?![\p{L}\p{N}_])/u,
  /(?<![\p{L}\p{N}_])history(?![\p{L}\p{N}_])/u,
  /(?<![\p{L}\p{N}_])previous(?:ly)?(?![\p{L}\p{N}_])/u,
  /(?<![\p{L}\p{N}_])formerly(?![\p{L}\p{N}_])/u,
  /(?<![\p{L}\p{N}_])antes(?![\p{L}\p{N}_])/u,
  /(?<![\p{L}\p{N}_])historia(?![\p{L}\p{N}_])/u,
  /(?<![\p{L}\p{N}_])avant(?![\p{L}\p{N}_])/u,
  /(?<![\p{L}\p{N}_])historique(?![\p{L}\p{N}_])/u,
  /以前/u,
  /昔/u,
  /履歴/u,
  /かつて/u,
  /前は/u,
  /(?<![\p{L}\p{N}_])(?:19|20)\d{2}(?![\p{L}\p{N}_])/u,
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function codePointCompare(left: string, right: string): number {
  const leftPoints = Array.from(left, (character) => character.codePointAt(0)!);
  const rightPoints = Array.from(
    right,
    (character) => character.codePointAt(0)!,
  );
  const sharedLength = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < sharedLength; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return leftPoints[index] - rightPoints[index];
    }
  }
  return leftPoints.length - rightPoints.length;
}

function unicodeCasefold(value: string): string {
  return value
    .normalize("NFKC")
    .toLowerCase()
    .replace(/ß/gu, "ss")
    .replace(/ς/gu, "σ");
}

function normalizeIdentifier(value: unknown): string {
  const normalized = unicodeCasefold(String(value || "")).trim();
  return normalized
    .split(/[^\p{L}\p{N}_]+/u)
    .filter(Boolean)
    .join("_");
}

function boundedStrength(value: unknown): number {
  if (
    (typeof value !== "number" && typeof value !== "string") ||
    (typeof value === "string" && value.trim() === "")
  ) {
    return 1;
  }
  const parsed = Number(value);
  if (Number.isNaN(parsed)) {
    return typeof value === "number" ||
      (typeof value === "string" && /^[+-]?nan$/iu.test(value.trim()))
      ? 0
      : 1;
  }
  return Math.max(0, Math.min(parsed, 1));
}

function hasOwn(value: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isPythonTruthy(value: unknown): boolean {
  if (value == null || value === false || value === 0 || value === "") {
    return false;
  }
  if (Array.isArray(value)) {
    return value.length > 0;
  }
  if (value instanceof Map || value instanceof Set) {
    return value.size > 0;
  }
  if (isRecord(value)) {
    return Object.keys(value).length > 0;
  }
  return true;
}

function iterableValues(value: unknown): unknown[] {
  if (
    typeof value === "string" ||
    value == null ||
    typeof (value as { [Symbol.iterator]?: unknown })[Symbol.iterator] !==
      "function"
  ) {
    return [];
  }
  return Array.from(value as Iterable<unknown>);
}

function isNonStringIterable(value: unknown): value is Iterable<unknown> {
  return (
    typeof value !== "string" &&
    value != null &&
    typeof (value as { [Symbol.iterator]?: unknown })[Symbol.iterator] ===
      "function"
  );
}

function scalarOrIterableIds(value: unknown): string[] {
  if (typeof value === "string" || typeof value === "number") {
    return [String(value)];
  }
  return iterableValues(value).filter(isPythonTruthy).map(String);
}

export function stripInternalMetadata(
  metadata?: Readonly<Record<string, unknown>> | null,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(metadata ?? {}).filter(
      ([key]) => !String(key).startsWith(internalPrefix),
    ),
  );
}

export function buildLifecycleMetadata(
  extraction: LifecycleExtraction,
  messages: readonly LifecycleMessage[],
): LifecycleMetadata {
  const evidence = extraction.evidence;
  if (!isRecord(evidence)) {
    throw new Error(
      "memory evidence must include a message_index and exact excerpt",
    );
  }

  const messageIndex = evidence.message_index;
  const excerpt = evidence.excerpt;
  if (
    typeof messageIndex !== "number" ||
    !Number.isInteger(messageIndex) ||
    messageIndex < 0 ||
    messageIndex >= messages.length
  ) {
    throw new Error("memory evidence message_index is invalid");
  }

  const message = messages[messageIndex];
  const content = message.content;
  if (message.role === "system" || typeof content !== "string") {
    throw new Error(
      "memory evidence must reference a non-system source message",
    );
  }
  if (
    typeof excerpt !== "string" ||
    excerpt.length === 0 ||
    !content.includes(excerpt)
  ) {
    throw new Error("memory evidence excerpt is not an exact source substring");
  }

  const kind = unicodeCasefold(String(extraction.kind || "event"));
  if (!memoryKinds.has(kind)) {
    throw new Error(`unsupported memory kind: ${kind}`);
  }

  const subject = normalizeIdentifier(extraction.subject);
  const predicate = normalizeIdentifier(extraction.predicate);
  const stateKey = subject && predicate ? `${subject}::${predicate}` : null;
  const metadata: LifecycleMetadata = {
    _mem0_status: "active",
    _mem0_kind: kind,
    _mem0_source_hashes: [
      createHash("sha256").update(excerpt, "utf8").digest("hex"),
    ],
    _mem0_source_message_indexes: [messageIndex],
    _mem0_evidence_strength: boundedStrength(
      hasOwn(extraction, "evidence_strength")
        ? extraction.evidence_strength
        : 1,
    ),
    _mem0_schema_version: schemaVersion,
  };
  if (stateKey && stateLikeKinds.has(kind)) {
    metadata._mem0_state_key = stateKey;
  }
  if (extraction.value != null) {
    metadata._mem0_value = extraction.value;
  }
  if (extraction.valid_from != null) {
    metadata._mem0_valid_from = extraction.valid_from;
  }
  if (extraction.valid_to != null) {
    metadata._mem0_valid_to = extraction.valid_to;
  }

  const relatedIds = isPythonTruthy(extraction.related_memory_ids)
    ? extraction.related_memory_ids
    : extraction.linked_memory_ids;
  const normalizedRelatedIds = [
    ...new Set(iterableValues(relatedIds).filter(isPythonTruthy).map(String)),
  ].sort(codePointCompare);
  if (isNonStringIterable(relatedIds)) {
    metadata._mem0_related_to = normalizedRelatedIds;
  }

  if (
    extraction.relation === "supersedes" ||
    extraction.relation === "conflicts_with" ||
    extraction.relation === "corroborates"
  ) {
    metadata._mem0_relation = extraction.relation;
  }
  return metadata;
}

export function unicodeLexicalTerms(text: string): string[] {
  const normalized = unicodeCasefold(text || "");
  const words = normalized.match(/[\p{L}\p{N}]+/gu) ?? [];
  const terms = [...words];
  const hasUnsegmentedScript =
    /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(
      normalized,
    );
  if (hasUnsegmentedScript) {
    const compact = Array.from(normalized).filter((character) =>
      /[\p{L}\p{N}]/u.test(character),
    );
    for (const width of [2, 3]) {
      for (let index = 0; index <= compact.length - width; index += 1) {
        terms.push(compact.slice(index, index + width).join(""));
      }
    }
  }
  return [...new Set(terms.filter(Boolean))];
}

function resultParts(result: LifecycleResult): ResultParts {
  const id = result.id == null ? null : String(result.id);
  const score = Number(result.score || 0);
  const payload = isRecord(result.payload) ? { ...result.payload } : {};
  return { id, score, payload };
}

function rowParts(row: LifecycleResult): [string, LifecyclePayload] {
  return [String(row.id), isRecord(row.payload) ? { ...row.payload } : {}];
}

export function lexicalFallbackResults(
  query: string,
  rows: Iterable<LifecycleResult>,
  options: { topK: number },
): FusedLifecycleResult[] {
  const queryTerms = new Set(unicodeLexicalTerms(query));
  if (queryTerms.size === 0) {
    return [];
  }
  const matches: FusedLifecycleResult[] = [];
  for (const row of rows) {
    const { id, payload } = resultParts(row);
    if (id === null) {
      continue;
    }
    const indexedText = String(payload.text_lemmatized || payload.data || "");
    const memoryTerms = new Set(unicodeLexicalTerms(indexedText));
    let overlap = 0;
    for (const term of queryTerms) {
      if (memoryTerms.has(term)) {
        overlap += 1;
      }
    }
    if (overlap > 0) {
      matches.push({ id, score: overlap / queryTerms.size, payload });
    }
  }
  matches.sort(
    (left, right) =>
      right.score - left.score || codePointCompare(left.id, right.id),
  );
  return matches.slice(0, options.topK);
}

export function isHistoricalQuery(query: string): boolean {
  const normalized = unicodeCasefold(query || "");
  return historicalPatterns.some((pattern) => pattern.test(normalized));
}

function isValidIsoDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/u.exec(value);
  if (!match) {
    return false;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 1 || month > 12 || day < 1) {
    return false;
  }
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return day <= daysInMonth;
}

export function historicalQueryWindow(query: string): HistoricalWindow | null {
  const normalized = (query || "").normalize("NFKC");
  const dateMatch =
    /(?<![\p{L}\p{N}_])((?:19|20)\d{2}-\d{2}-\d{2})(?![\p{L}\p{N}_])/u.exec(
      normalized,
    );
  if (dateMatch) {
    return isValidIsoDate(dateMatch[1]) ? [dateMatch[1], dateMatch[1]] : null;
  }
  const yearMatch = /(?<![\p{L}\p{N}_])((?:19|20)\d{2})(?![\p{L}\p{N}_])/u.exec(
    normalized,
  );
  if (!yearMatch) {
    return null;
  }
  return [`${yearMatch[1]}-01-01`, `${yearMatch[1]}-12-31`];
}

function payloadDate(value: unknown): string | null {
  if (!isPythonTruthy(value)) {
    return null;
  }
  const match = /^(\d{4}-\d{2}-\d{2})(?:$|[T ])/u.exec(String(value));
  return match && isValidIsoDate(match[1]) ? match[1] : null;
}

function payloadOverlapsWindow(
  payload: LifecyclePayload,
  window: HistoricalWindow,
): boolean {
  const [windowStart, windowEnd] = window;
  const validFrom = payloadDate(payload._mem0_valid_from);
  const validTo = payloadDate(payload._mem0_valid_to);
  return (
    (validFrom === null || validFrom <= windowEnd) &&
    (validTo === null || validTo >= windowStart)
  );
}

function contentKey(payload: LifecyclePayload): string {
  return unicodeLexicalTerms(String(payload.data || "")).join(" ");
}

function recallEligible(
  payload: LifecyclePayload,
  historical: boolean,
  historicalWindow?: HistoricalWindow | null,
): boolean {
  const status = String(payload._mem0_status || "active");
  if (
    suppressedStatuses.has(status) ||
    (status === "superseded" && !historical)
  ) {
    return false;
  }
  if (
    historical &&
    historicalWindow &&
    stateLikeKinds.has(String(payload._mem0_kind))
  ) {
    return payloadOverlapsWindow(payload, historicalWindow);
  }
  return true;
}

interface MatchBlock {
  a: number;
  b: number;
  size: number;
}

function sequenceMatchRatio(left: string, right: string): number {
  const a = Array.from(left);
  const b = Array.from(right);
  if (a.length + b.length === 0) {
    return 1;
  }

  const positions = new Map<string, number[]>();
  for (let index = 0; index < b.length; index += 1) {
    const bucket = positions.get(b[index]) ?? [];
    bucket.push(index);
    positions.set(b[index], bucket);
  }
  if (b.length >= 200) {
    const popularityThreshold = Math.floor(b.length / 100) + 1;
    for (const [character, indexes] of positions) {
      if (indexes.length > popularityThreshold) {
        positions.delete(character);
      }
    }
  }

  const findLongestMatch = (
    aStart: number,
    aEnd: number,
    bStart: number,
    bEnd: number,
  ): MatchBlock => {
    let bestA = aStart;
    let bestB = bStart;
    let bestSize = 0;
    let previous = new Map<number, number>();
    for (let aIndex = aStart; aIndex < aEnd; aIndex += 1) {
      const current = new Map<number, number>();
      for (const bIndex of positions.get(a[aIndex]) ?? []) {
        if (bIndex < bStart) {
          continue;
        }
        if (bIndex >= bEnd) {
          break;
        }
        const size = (previous.get(bIndex - 1) ?? 0) + 1;
        current.set(bIndex, size);
        if (size > bestSize) {
          bestA = aIndex - size + 1;
          bestB = bIndex - size + 1;
          bestSize = size;
        }
      }
      previous = current;
    }
    while (bestA > aStart && bestB > bStart && a[bestA - 1] === b[bestB - 1]) {
      bestA -= 1;
      bestB -= 1;
      bestSize += 1;
    }
    while (
      bestA + bestSize < aEnd &&
      bestB + bestSize < bEnd &&
      a[bestA + bestSize] === b[bestB + bestSize]
    ) {
      bestSize += 1;
    }
    return { a: bestA, b: bestB, size: bestSize };
  };

  const queue: Array<[number, number, number, number]> = [
    [0, a.length, 0, b.length],
  ];
  const blocks: MatchBlock[] = [];
  while (queue.length > 0) {
    const [aStart, aEnd, bStart, bEnd] = queue.pop()!;
    const match = findLongestMatch(aStart, aEnd, bStart, bEnd);
    if (match.size === 0) {
      continue;
    }
    blocks.push(match);
    if (aStart < match.a && bStart < match.b) {
      queue.push([aStart, match.a, bStart, match.b]);
    }
    if (match.a + match.size < aEnd && match.b + match.size < bEnd) {
      queue.push([match.a + match.size, aEnd, match.b + match.size, bEnd]);
    }
  }
  const matches = blocks.reduce((total, block) => total + block.size, 0);
  return (2 * matches) / (a.length + b.length);
}

export function reciprocalRankFusion(
  channels: RrfChannels,
  options: RrfOptions,
): FusedLifecycleResult[] {
  const candidates = new Map<string, FusionCandidate>();
  let activeWeights = 0;
  for (const channel of Object.keys(rrfWeights) as RrfChannel[]) {
    const weight = rrfWeights[channel];
    const results: ResultParts[] = [];
    for (const rawResult of channels[channel] ?? []) {
      const parts = resultParts(rawResult);
      if (
        recallEligible(
          parts.payload,
          options.historical,
          options.historicalWindow,
        )
      ) {
        results.push(parts);
      }
    }
    if (results.length === 0) {
      continue;
    }
    activeWeights += weight;
    const seenInChannel = new Set<string>();
    for (let index = 0; index < results.length; index += 1) {
      const { id, score: sourceScore, payload } = results[index];
      const rank = index + 1;
      if (id === null || seenInChannel.has(id)) {
        continue;
      }
      seenInChannel.add(id);
      let candidate = candidates.get(id);
      if (!candidate) {
        candidate = {
          id,
          payload,
          fusion: 0,
          ranks: {},
          sourceScores: {},
          score: 0,
          evidenceTiebreak: 0,
        };
        candidates.set(id, candidate);
      }
      if (Object.keys(candidate.payload).length === 0) {
        candidate.payload = payload;
      }
      candidate.fusion += weight / (rrfConstant + rank);
      candidate.ranks[channel] = rank;
      candidate.sourceScores[channel] = sourceScore;
    }
  }

  if (candidates.size === 0 || activeWeights === 0) {
    return [];
  }
  const maximum = activeWeights / (rrfConstant + 1);
  const eligible: FusionCandidate[] = [];
  for (const candidate of candidates.values()) {
    candidate.score = Math.min(candidate.fusion / maximum, 1);
    if (candidate.score < options.threshold) {
      continue;
    }
    candidate.evidenceTiebreak =
      boundedStrength(
        hasOwn(candidate.payload, "_mem0_evidence_strength")
          ? candidate.payload._mem0_evidence_strength
          : 0.5,
      ) * 0.001;
    eligible.push(candidate);
  }
  eligible.sort(
    (left, right) =>
      right.score - left.score ||
      right.evidenceTiebreak - left.evidenceTiebreak ||
      codePointCompare(left.id, right.id),
  );

  const selected: FusedLifecycleResult[] = [];
  const seenStateKeys = new Map<string, string>();
  const seenContent = new Map<string, string>();
  let charCount = 0;
  for (const candidate of eligible) {
    const payload = candidate.payload;
    const status = String(payload._mem0_status || "active");
    const stateKey = isPythonTruthy(payload._mem0_state_key)
      ? String(payload._mem0_state_key)
      : null;
    const candidateContentKey = contentKey(payload);
    let deduplicationReason: string | null = null;
    let canonicalId: string | null = null;

    if (
      !options.historical &&
      status !== "disputed" &&
      stateKey &&
      seenStateKeys.has(stateKey)
    ) {
      canonicalId = seenStateKeys.get(stateKey)!;
      deduplicationReason = `state_key:${stateKey}`;
    } else if (candidateContentKey && payload._mem0_kind !== "event") {
      for (const [priorKey, priorId] of seenContent) {
        if (
          candidateContentKey === priorKey ||
          sequenceMatchRatio(candidateContentKey, priorKey) >= 0.98
        ) {
          canonicalId = priorId;
          deduplicationReason = "near_identical";
          break;
        }
      }
    }
    if (deduplicationReason) {
      if (options.explain && canonicalId) {
        const canonical = selected.find((item) => item.id === canonicalId);
        if (canonical?.scoreDetails) {
          canonical.scoreDetails.deduplicatedCandidates ??= [];
          canonical.scoreDetails.deduplicatedCandidates.push({
            id: candidate.id,
            reason: deduplicationReason,
          });
          canonical.scoreDetails.deduplicationReason = "canonical_result";
        }
      }
      continue;
    }

    const text = String(payload.data || "");
    const textLength = Array.from(text).length;
    if (options.maxChars != null && charCount + textLength > options.maxChars) {
      continue;
    }
    const item: FusedLifecycleResult = {
      id: candidate.id,
      score: candidate.score,
      payload,
    };
    if (options.explain) {
      item.scoreDetails = {
        fusionRanks: candidate.ranks,
        fusionSourceScores: candidate.sourceScores,
        stateDisposition:
          status === "superseded"
            ? "historical"
            : status === "disputed"
              ? "disputed"
              : "current",
        deduplicationReason: null,
        evidenceStrengthFactor: candidate.evidenceTiebreak,
        finalScore: candidate.score,
        threshold: options.threshold,
      };
    }
    selected.push(item);
    charCount += textLength;
    if (stateKey && status !== "disputed") {
      seenStateKeys.set(stateKey, candidate.id);
    }
    if (candidateContentKey) {
      seenContent.set(candidateContentKey, candidate.id);
    }
    if (selected.length >= options.topK) {
      break;
    }
  }
  return selected;
}

function localIsoDate(): string {
  const now = new Date();
  const year = String(now.getFullYear()).padStart(4, "0");
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function auditRows(rows: Iterable<LifecycleResult>): AuditReport {
  const records = Array.from(rows, rowParts);
  const ids = new Set(records.map(([id]) => id));
  const supersededTargets = new Set<string>();
  for (const [, payload] of records) {
    for (const target of scalarOrIterableIds(payload._mem0_supersedes)) {
      supersededTargets.add(target);
    }
  }

  const contentGroups = new Map<string, string[]>();
  const orphanedLinks: string[] = [];
  const providerInconsistencies: string[] = [];
  for (const [memoryId, payload] of records) {
    const key = contentKey(payload);
    if (key) {
      const group = contentGroups.get(key) ?? [];
      group.push(memoryId);
      contentGroups.set(key, group);
    }
    if (!isPythonTruthy(payload.data) || !isPythonTruthy(payload.hash)) {
      providerInconsistencies.push(memoryId);
    }
    const linkedIds = new Set<string>();
    for (const field of [
      "_mem0_supersedes",
      "_mem0_conflicts_with",
      "_mem0_superseded_by",
      "_mem0_replacement_id",
    ]) {
      for (const linkedId of scalarOrIterableIds(payload[field])) {
        linkedIds.add(linkedId);
      }
    }
    if ([...linkedIds].some((linkedId) => !ids.has(linkedId))) {
      orphanedLinks.push(memoryId);
    }
  }

  const duplicates = [...contentGroups.values()]
    .filter((group) => group.length > 1)
    .map((group) => [...group].sort(codePointCompare))
    .sort((left, right) => {
      const sharedLength = Math.min(left.length, right.length);
      for (let index = 0; index < sharedLength; index += 1) {
        const comparison = codePointCompare(left[index], right[index]);
        if (comparison !== 0) {
          return comparison;
        }
      }
      return left.length - right.length;
    });

  const issues: AuditIssues = {
    duplicates,
    staleActivePredecessors: records
      .filter(
        ([memoryId, payload]) =>
          supersededTargets.has(memoryId) &&
          (payload._mem0_status || "active") === "active",
      )
      .map(([memoryId]) => memoryId)
      .sort(codePointCompare),
    disputedChains: records
      .filter(([, payload]) => payload._mem0_status === "disputed")
      .map(([memoryId]) => memoryId)
      .sort(codePointCompare),
    ungroundedLegacy: records
      .filter(([, payload]) => payload._mem0_schema_version == null)
      .map(([memoryId]) => memoryId)
      .sort(codePointCompare),
    expired: records
      .filter(
        ([, payload]) =>
          isPythonTruthy(payload.expiration_date) &&
          String(payload.expiration_date) < localIsoDate(),
      )
      .map(([memoryId]) => memoryId)
      .sort(codePointCompare),
    orphanedLinks: [...new Set(orphanedLinks)].sort(codePointCompare),
    providerInconsistencies: [...new Set(providerInconsistencies)].sort(
      codePointCompare,
    ),
  };
  return {
    scanned: records.length,
    summary: {
      duplicates: issues.duplicates.length,
      staleActivePredecessors: issues.staleActivePredecessors.length,
      disputedChains: issues.disputedChains.length,
      ungroundedLegacy: issues.ungroundedLegacy.length,
      expired: issues.expired.length,
      orphanedLinks: issues.orphanedLinks.length,
      providerInconsistencies: issues.providerInconsistencies.length,
    },
    issues,
  };
}

function stableValueKey(value: unknown): string {
  if (value === null || value === undefined) {
    return "null";
  }
  if (typeof value === "string") {
    return `string:${JSON.stringify(value)}`;
  }
  if (typeof value === "number") {
    return `number:${String(value)}`;
  }
  if (typeof value === "boolean") {
    return `boolean:${value}`;
  }
  if (Array.isArray(value)) {
    return `array:[${value.map(stableValueKey).join(",")}]`;
  }
  if (isRecord(value)) {
    return `object:{${Object.keys(value)
      .map((key) => `${JSON.stringify(key)}:${stableValueKey(value[key])}`)
      .join(",")}}`;
  }
  return `${typeof value}:${String(value)}`;
}

export function safeConsolidationActions(
  rows: Iterable<LifecycleResult>,
): ConsolidationAction[] {
  const records = Array.from(rows, rowParts);
  const byContent = new Map<string, Array<[string, LifecyclePayload]>>();
  for (const record of records) {
    const payload = record[1];
    if (
      payload._mem0_schema_version !== schemaVersion ||
      payload._mem0_kind === "event" ||
      payload._mem0_status === "disputed"
    ) {
      continue;
    }
    const key = contentKey(payload);
    const kind = payload._mem0_kind;
    const stateKey = payload._mem0_state_key;
    if (!key || !isPythonTruthy(kind) || !isPythonTruthy(stateKey)) {
      continue;
    }
    const duplicateKey = [
      "lifecycle",
      String(kind),
      String(stateKey),
      stableValueKey(payload._mem0_value),
      key,
    ].join("\u0000");
    const group = byContent.get(duplicateKey) ?? [];
    group.push(record);
    byContent.set(duplicateKey, group);
  }

  const actions: ConsolidationAction[] = [];
  const actedOn = new Set<string>();
  const statusPriority: Record<string, number> = {
    active: 0,
    disputed: 1,
    superseded: 2,
    archived: 3,
    invalid: 4,
  };
  for (const group of byContent.values()) {
    if (group.length < 2) {
      continue;
    }
    const ordered = [...group].sort((left, right) => {
      const leftStatus = String(left[1]._mem0_status || "active");
      const rightStatus = String(right[1]._mem0_status || "active");
      const priorityDifference =
        (statusPriority[leftStatus] ?? 5) - (statusPriority[rightStatus] ?? 5);
      if (priorityDifference !== 0) {
        return priorityDifference;
      }
      const dateDifference = codePointCompare(
        String(left[1].created_at || ""),
        String(right[1].created_at || ""),
      );
      return dateDifference || codePointCompare(left[0], right[0]);
    });
    const canonicalId = ordered[0][0];
    for (const [duplicateId] of ordered.slice(1)) {
      actions.push({
        memoryId: duplicateId,
        action: "archive",
        reason: `exact_duplicate:${canonicalId}`,
      });
      actedOn.add(duplicateId);
    }
  }

  const supersededTargets = new Set<string>();
  for (const [, payload] of records) {
    for (const target of scalarOrIterableIds(payload._mem0_supersedes)) {
      supersededTargets.add(target);
    }
  }
  for (const [memoryId, payload] of [...records].sort((left, right) =>
    codePointCompare(left[0], right[0]),
  )) {
    if (
      supersededTargets.has(memoryId) &&
      !actedOn.has(memoryId) &&
      payload._mem0_schema_version === schemaVersion &&
      (payload._mem0_status || "active") === "active"
    ) {
      actions.push({
        memoryId,
        action: "supersede",
        reason: "linked_successor",
      });
    }
  }
  return actions;
}

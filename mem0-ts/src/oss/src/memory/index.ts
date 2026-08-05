import { v4 as uuidv4 } from "uuid";
import { createHash } from "crypto";
import {
  MemoryConfig,
  MemoryConfigSchema,
  MemoryItem,
  Message,
  SearchFilters,
  SearchResult,
  VectorStoreConfig,
  VectorStoreResult,
} from "../types";
import {
  EmbedderFactory,
  LLMFactory,
  VectorStoreFactory,
  HistoryManagerFactory,
  RerankerFactory,
} from "../utils/factory";
import {
  FactRetrievalSchema,
  getFactRetrievalMessages,
  getUpdateMemoryMessages,
  parseMessages,
  extractJson,
  ADDITIVE_EXTRACTION_PROMPT,
  AGENT_CONTEXT_SUFFIX,
  AdditiveExtractionSchema,
  generateAdditiveExtractionPrompt,
} from "../prompts";
import { DummyHistoryManager } from "../storage/DummyHistoryManager";
import { Embedder } from "../embeddings/base";
import { LLM } from "../llms/base";
import { Reranker } from "../rerankers/base";
import { VectorStore } from "../vector_stores/base";
import { ConfigManager } from "../config/manager";

import {
  AddMemoryOptions,
  SearchMemoryOptions,
  DeleteAllMemoryOptions,
  GetAllMemoryOptions,
  UpdateMemoryOptions,
  UpdateProjectOptions,
  QualityOperationOptions,
  ConsolidateMemoryOptions,
  FeedbackMemoryOptions,
} from "./memory.types";
import {
  auditRows,
  buildLifecycleMetadata,
  historicalQueryWindow,
  internalPrefix,
  isHistoricalQuery,
  lexicalFallbackResults,
  reciprocalRankFusion,
  safeConsolidationActions,
  schemaVersion,
  stateLikeKinds,
  stripInternalMetadata,
} from "./lifecycle";
import { parse_vision_messages } from "../utils/memory";
import { HistoryManager } from "../storage/base";
import { captureClientEvent } from "../utils/telemetry";
import {
  detectScaleThresholdFromAddResult,
  detectScaleThresholdFromTopK,
  detectPerformanceSlowQuery,
  detectTemporalUsageFromMetadata,
  detectTemporalUsageFromSearch,
  displayDecayUsageNotice,
  displayFirstRunNotice,
  displayPerformanceSlowQueryNotice,
  displayScaleThresholdNotice,
  displayTemporalUsageNotice,
  getDecayFeatureErrorMessage,
  getDecayUsageDeleteCountAfterSuccess,
  getTemporalFeatureErrorMessage,
  isDecayUsageDeleteEligible,
  PerformanceSlowQueryTrigger,
  ScaleThresholdTrigger,
} from "../utils/notices";
import { lemmatizeForBm25 } from "../utils/lemmatization";
import {
  extractEntities,
  extractEntitiesBatch,
} from "../utils/entity_extraction";
import {
  scoreAndRank,
  getBm25Params,
  normalizeBm25,
  ENTITY_BOOST_WEIGHT,
} from "../utils/scoring";
import { getDefaultVectorStoreDbPath } from "../utils/sqlite";
import { logger } from "../utils/logger";
import { normalizeExpirationDate, payloadIsExpired } from "../utils/expiration";
import { getOrCreateMem0UserId } from "../../../client/config";

export class LLMError extends Error {
  readonly cause?: unknown;

  constructor(message: string, options: { cause?: unknown } = {}) {
    super(message);
    this.name = "LLMError";
    this.cause = options.cause;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export class MemoryPersistenceError extends Error {
  readonly plannedIds: string[];
  readonly cause?: unknown;

  constructor(
    message: string,
    options: { plannedIds?: string[]; cause?: unknown } = {},
  ) {
    super(message);
    this.name = "MemoryPersistenceError";
    this.plannedIds = options.plannedIds ?? [];
    this.cause = options.cause;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

// Entity params that must be passed via filters check both snake_case and camelCase
const ENTITY_PARAMS = [
  "user_id",
  "agent_id",
  "run_id",
  "userId",
  "agentId",
  "runId",
];

// Identity keys stripped from update() metadata: ENTITY_PARAMS covers user_id/agent_id/run_id
// in both casings (the default store promotes camelCase on read); actor_id has no camelCase alias.
const IDENTITY_KEYS = [...ENTITY_PARAMS, "actor_id"];

// Batch size for deleteAll pagination. Larger than most vector store default
// page limits (~100) to minimize roundtrips while bounded to avoid memory pressure.
const DELETE_ALL_BATCH_SIZE = 1000;

/**
 * Validates that no top-level entity parameters are passed in config.
 * @throws Error if entity params are found at top level
 */
function rejectTopLevelEntityParams(
  config: Record<string, any>,
  methodName: string,
): void {
  const invalidKeys = Object.keys(config).filter((k) =>
    ENTITY_PARAMS.includes(k),
  );
  if (invalidKeys.length > 0) {
    throw new Error(
      `Top-level entity parameters [${invalidKeys.join(", ")}] are not supported in ${methodName}(). ` +
        `Use filters: { userId: "..." } instead.`,
    );
  }
}

/**
 * Validates and normalizes an entity ID.
 * - Coerces non-string ids (e.g. numeric database keys) to string
 * - Trims leading/trailing whitespace
 * - Rejects empty or whitespace-only strings
 * - Rejects strings containing internal whitespace
 * @returns The trimmed entity ID, or undefined if input is undefined/null
 * @throws Error if entity ID is invalid
 */
function validateAndTrimEntityId(
  value: string | number | undefined | null,
  name: string,
): string | undefined {
  if (value == null) return undefined;
  const trimmed = String(value).trim();
  if (trimmed === "") {
    throw new Error(
      `Invalid ${name}: cannot be empty or whitespace-only. Provide a valid identifier.`,
    );
  }
  if (/\s/.test(trimmed)) {
    throw new Error(
      `Invalid ${name}: cannot contain whitespace. Provide a valid identifier without spaces.`,
    );
  }
  return trimmed;
}

/**
 * Validates search parameters.
 * @throws Error if threshold or topK are invalid
 */
function validateSearchParams(
  threshold?: number,
  topK?: number,
  maxChars?: number,
): void {
  if (threshold !== undefined) {
    if (typeof threshold !== "number" || isNaN(threshold)) {
      throw new Error("threshold must be a valid number");
    }
    if (threshold < 0 || threshold > 1) {
      throw new Error(
        `Invalid threshold: ${threshold}. Must be between 0 and 1 (inclusive).`,
      );
    }
  }
  if (topK !== undefined) {
    if (typeof topK !== "number" || isNaN(topK) || !Number.isInteger(topK)) {
      throw new Error("topK must be a valid integer");
    }
    if (topK < 0) {
      throw new Error(`Invalid topK: ${topK}. Must be a non-negative integer.`);
    }
  }
  if (maxChars !== undefined) {
    if (
      typeof maxChars !== "number" ||
      isNaN(maxChars) ||
      !Number.isInteger(maxChars) ||
      maxChars < 0
    ) {
      throw new Error("maxChars must be a non-negative integer");
    }
  }
}

export class Memory {
  private config: MemoryConfig;
  private customInstructions: string | undefined;
  private embedder: Embedder;
  private vectorStore!: VectorStore;
  private llm: LLM;
  private reranker: Reranker | null = null;
  private db: HistoryManager;
  private collectionName: string | undefined;
  private apiVersion: string;
  telemetryId: string;
  private _initPromise: Promise<void>;
  private _initError?: Error;
  private _entityStore?: VectorStore;

  constructor(config: Partial<MemoryConfig> = {}) {
    // Merge and validate config
    this.config = ConfigManager.mergeConfig(config);

    this.customInstructions = this.config.customInstructions;
    this.embedder = EmbedderFactory.create(
      this.config.embedder.provider,
      this.config.embedder.config,
    );
    // Vector store creation is deferred to _autoInitialize() so that
    // the embedding dimension can be auto-detected first when not
    // explicitly configured.
    this.llm = LLMFactory.create(
      this.config.llm.provider,
      this.config.llm.config,
    );
    if (this.config.reranker) {
      this.reranker = RerankerFactory.create(
        this.config.reranker.provider,
        this.config.reranker.config,
      );
    }
    if (this.config.disableHistory) {
      this.db = new DummyHistoryManager();
    } else {
      this.db = HistoryManagerFactory.create(
        this.config.historyStore!.provider,
        this.config.historyStore!,
      );
    }

    this.collectionName = this.config.vectorStore.config.collectionName;
    this.apiVersion = this.config.version || "v1.0";
    this.telemetryId = "anonymous";

    // Auto-detect embedding dimension (if needed), create vector store,
    // and initialize it. All public methods await this before proceeding.
    this._initPromise = this._autoInitialize().catch((error) => {
      this._initError =
        error instanceof Error ? error : new Error(String(error));
      console.error(this._initError);
    });
  }

  /**
   * If no explicit dimension was provided, runs a probe embedding to
   * detect it. Then creates and initializes the vector store.
   */
  private async _autoInitialize(): Promise<void> {
    if (!this.config.vectorStore.config.dimension) {
      try {
        const probe = await this.embedder.embed("dimension probe");
        this.config.vectorStore.config.dimension = probe.length;
      } catch (error: any) {
        throw new Error(
          `Failed to auto-detect embedding dimension from provider '${this.config.embedder.provider}': ${error.message}. ` +
            `Please set 'dimension' in vectorStore.config or 'embeddingDims' in embedder.config explicitly.`,
        );
      }
    }

    this.vectorStore = VectorStoreFactory.create(
      this.config.vectorStore.provider,
      this.config.vectorStore.config,
    );

    // The vector store constructor may fire initialize() asynchronously
    // (e.g. Qdrant). Explicitly await it here to guarantee the backing
    // store (collections, tables, etc.) is ready before any public method
    // attempts to read or write.
    await this.vectorStore.initialize();

    await this._initializeTelemetry();
  }

  /**
   * Ensures that auto-initialization (dimension detection + vector store
   * creation) has completed before any public method proceeds.
   * If a previous init attempt failed, retries automatically.
   */
  private async _ensureInitialized(): Promise<void> {
    await this._initPromise;
    if (this._initError) {
      // Clear failed state and retry — the embedder or vector store
      // may have been transiently unavailable at startup.
      this._initError = undefined;
      this._initPromise = this._autoInitialize().catch((error) => {
        this._initError =
          error instanceof Error ? error : new Error(String(error));
        console.error(this._initError);
      });
      await this._initPromise;
      if (this._initError) {
        throw this._initError;
      }
    }
  }

  private async getEntityStore(): Promise<VectorStore> {
    if (!this._entityStore) {
      const entityProvider = this.config.vectorStore.provider;
      const entityCollectionName = `${this.collectionName}_entities`;
      const entityConfig: VectorStoreConfig = {
        ...this.config.vectorStore.config,
        collectionName: entityCollectionName,
      };
      // For file-based stores (memory/SQLite), always use a separate DB for entities
      if (entityProvider === "memory") {
        const basePath = entityConfig.dbPath || getDefaultVectorStoreDbPath();
        entityConfig.dbPath = basePath.replace(/\.db$/, "_entities.db");
      }
      if (entityProvider === "databricks") {
        entityConfig.tableName = entityConfig.tableName
          ? `${entityConfig.tableName}_entities`
          : entityCollectionName;
      }
      this._entityStore = VectorStoreFactory.create(
        entityProvider,
        entityConfig,
      );
      await this._entityStore.initialize();
    }
    return this._entityStore;
  }

  /**
   * Normalize a filters object for entity-store scoping: keeps only
   * user_id/agent_id/run_id keys whose values are defined.
   */
  private _sessionFiltersFromPayload(
    payload: Record<string, any>,
  ): Record<string, any> {
    const filters: Record<string, any> = {};
    if (payload.user_id) filters.user_id = payload.user_id;
    if (payload.agent_id) filters.agent_id = payload.agent_id;
    if (payload.run_id) filters.run_id = payload.run_id;
    return filters;
  }

  private _normalizeEntityText(value: string): string {
    return value.trim().toLowerCase().replace(/\s+/g, " ");
  }

  private async _existingEntitiesByText(
    entityStore: VectorStore,
    filters: Record<string, any>,
  ): Promise<Map<string, { id: string; payload: Record<string, any> }>> {
    const rowsByText = new Map<
      string,
      { id: string; payload: Record<string, any> }
    >();
    let rows: Array<{ id: string; payload: Record<string, any> }> = [];
    try {
      const listed = await entityStore.list(filters, 10000);
      rows = (
        Array.isArray(listed) && Array.isArray(listed[0])
          ? listed[0]
          : (listed as any)
      ) as Array<{ id: string; payload: Record<string, any> }>;
    } catch (e) {
      console.debug(
        `Exact entity lookup failed, falling back to semantic dedup: ${e}`,
      );
      return rowsByText;
    }

    for (const row of rows) {
      const text = row.payload?.data;
      if (typeof text !== "string") continue;
      const key = this._normalizeEntityText(text);
      if (key && !rowsByText.has(key)) {
        rowsByText.set(key, row);
      }
    }
    return rowsByText;
  }

  /**
   * Remove `memoryId` from every entity record scoped to `filters`.
   * If an entity's `linkedMemoryIds` becomes empty after removal, the
   * entity record itself is deleted. Errors on individual entities are
   * swallowed so one bad record does not break the whole operation.
   *
   * No-op if the entity store has not been initialized yet.
   */
  private async _removeMemoryFromEntityStore(
    memoryId: string,
    filters: Record<string, any>,
  ): Promise<void> {
    let entityStore: VectorStore;
    try {
      entityStore = await this.getEntityStore();
    } catch (e) {
      console.debug(`Entity store unavailable during cleanup: ${e}`);
      return;
    }

    let rows: Array<{ id: string; payload: Record<string, any> }> = [];
    try {
      const listed = await entityStore.list(filters, 10000);
      rows = (
        Array.isArray(listed) && Array.isArray(listed[0])
          ? listed[0]
          : (listed as any)
      ) as Array<{ id: string; payload: Record<string, any> }>;
    } catch (e) {
      console.debug(`Entity store list failed during cleanup: ${e}`);
      return;
    }

    for (const row of rows) {
      try {
        const payload = row.payload || {};
        const linked: string[] = Array.isArray(payload.linkedMemoryIds)
          ? payload.linkedMemoryIds
          : [];
        if (!linked.includes(memoryId)) continue;

        const remaining = linked.filter((id) => id !== memoryId);
        if (remaining.length === 0) {
          try {
            await entityStore.delete(row.id);
          } catch (e) {
            console.debug(`Entity delete failed for id=${row.id}: ${e}`);
          }
        } else {
          const newPayload = { ...payload, linkedMemoryIds: remaining };
          // entityStore.update requires a vector — re-embed entity text.
          const entityText =
            typeof payload.data === "string" ? payload.data : "";
          if (!entityText) {
            // Can't re-embed without text; skip gracefully.
            console.debug(
              `Entity id=${row.id} missing 'data'; skipping update during cleanup`,
            );
            continue;
          }
          let vec: number[];
          try {
            vec = await this.embedder.embed(entityText, "update");
          } catch (e) {
            console.debug(`Entity re-embed failed for '${entityText}': ${e}`);
            continue;
          }
          try {
            await entityStore.update(row.id, vec, newPayload);
          } catch (e) {
            console.debug(`Entity update failed for id=${row.id}: ${e}`);
          }
        }
      } catch (e) {
        console.debug(`Entity cleanup error for id=${row?.id}: ${e}`);
      }
    }
  }

  /**
   * Extract entities from `text` and link them to `memoryId` in the
   * entity store, scoped to `filters` (user_id / agent_id / run_id).
   *
   * Simpler single-memory variant of Phase 7 in add(): no cross-memory
   * dedup, but still does per-entity "search for existing, update if
   * match >= 0.95 else insert new". Non-fatal errors are swallowed.
   */
  private async _linkEntitiesForMemory(
    memoryId: string,
    text: string,
    filters: Record<string, any>,
  ): Promise<void> {
    try {
      const entities = extractEntities(text);
      if (entities.length === 0) return;

      const entityStore = await this.getEntityStore();
      const exactMatches = await this._existingEntitiesByText(
        entityStore,
        filters,
      );

      for (const entity of entities) {
        try {
          let entityVec: number[];
          try {
            entityVec = await this.embedder.embed(entity.text, "add");
          } catch (e) {
            console.debug(`Entity embed failed for '${entity.text}': ${e}`);
            continue;
          }

          let matches: Array<{
            id: string;
            score?: number;
            payload: Record<string, any>;
          }> = [];
          const exactMatch = exactMatches.get(
            this._normalizeEntityText(entity.text),
          );
          if (!exactMatch) {
            try {
              matches = await entityStore.search(entityVec, 1, filters);
            } catch {}
          }

          const semanticMatch =
            matches.length > 0 && (matches[0].score ?? 0) >= 0.95
              ? matches[0]
              : undefined;
          const match = exactMatch ?? semanticMatch;
          if (match) {
            const payload = match.payload || {};
            const linked = new Set<string>(
              Array.isArray(payload.linkedMemoryIds)
                ? payload.linkedMemoryIds
                : [],
            );
            linked.add(memoryId);
            payload.linkedMemoryIds = Array.from(linked).sort();
            try {
              await entityStore.update(match.id, entityVec, payload);
            } catch (e) {
              console.debug(`Entity update failed for '${entity.text}': ${e}`);
            }
          } else {
            const entityPayload: Record<string, any> = {
              data: entity.text,
              entityType: entity.type,
              linkedMemoryIds: [memoryId],
            };
            if (filters.user_id) entityPayload.user_id = filters.user_id;
            if (filters.agent_id) entityPayload.agent_id = filters.agent_id;
            if (filters.run_id) entityPayload.run_id = filters.run_id;

            try {
              await entityStore.insert(
                [entityVec],
                [uuidv4()],
                [entityPayload],
              );
            } catch (e) {
              console.debug(`Entity insert failed for '${entity.text}': ${e}`);
            }
          }
        } catch (e) {
          console.debug(`Entity link error for '${entity.text}': ${e}`);
        }
      }
    } catch (e) {
      console.warn(`Entity linking failed during update: ${e}`);
    }
  }

  private buildSessionScope(filters: SearchFilters): string {
    const parts: string[] = [];
    for (const key of ["agent_id", "run_id", "user_id"].sort()) {
      const val = (filters as any)[key];
      if (val) parts.push(`${key}=${val}`);
    }
    return parts.join("&");
  }

  private async _initializeTelemetry() {
    try {
      await this._getTelemetryId();

      // Capture initialization event
      await captureClientEvent("init", this, {
        api_version: this.apiVersion,
        client_type: "Memory",
        collection_name: this.collectionName,
      });
    } catch (error) {}
  }

  private async _getTelemetryId() {
    try {
      if (
        !this.telemetryId ||
        this.telemetryId === "anonymous" ||
        this.telemetryId === "anonymous-supabase"
      ) {
        this.telemetryId =
          (await getOrCreateMem0UserId()) ||
          (await this.vectorStore.getUserId());
        try {
          await this.vectorStore.setUserId(this.telemetryId);
        } catch {}
      }
      return this.telemetryId;
    } catch (error) {
      this.telemetryId = "anonymous";
      return this.telemetryId;
    }
  }

  private async _captureEvent(methodName: string, additionalData = {}) {
    try {
      await this._getTelemetryId();
      await captureClientEvent(methodName, this, {
        ...additionalData,
        api_version: this.apiVersion,
        collection_name: this.collectionName,
      });
    } catch (error) {
      console.error(`Failed to capture ${methodName} event:`, error);
    }
  }

  private async _displayFirstRunNotice(triggerFunction: string) {
    try {
      await this._getTelemetryId();
      await displayFirstRunNotice(this, triggerFunction);
    } catch {}
  }

  private async _displayDecayUsageNotice(trigger: {
    triggerFunction: "delete" | "delete_all";
    triggerSource: "delete_count" | "delete_all";
    triggerReason: "repeated_deletes" | "bulk_delete";
    deleteCount?: number;
    deletedCount?: number;
  }) {
    try {
      await this._getTelemetryId();
      await displayDecayUsageNotice(this, trigger);
    } catch {}
  }

  private async _displayTemporalUsageNotice(trigger: {
    triggerFunction: "add" | "search";
    triggerSource: "metadata" | "query" | "filter";
    triggerReason:
      | "date_like_metadata"
      | "relative_phrase"
      | "date_like_query"
      | "date_range_filter";
  }) {
    try {
      await this._getTelemetryId();
      await displayTemporalUsageNotice(this, trigger);
    } catch {}
  }

  private async _displayScaleThresholdNotice(trigger: ScaleThresholdTrigger) {
    try {
      await this._getTelemetryId();
      await displayScaleThresholdNotice(this, trigger);
    } catch {}
  }

  private async _displayPerformanceSlowQueryNotice(
    trigger: PerformanceSlowQueryTrigger,
  ) {
    try {
      await this._getTelemetryId();
      await displayPerformanceSlowQueryNotice(this, trigger);
    } catch {}
  }

  private async _getNoticeTelemetryId() {
    try {
      if (
        !this.telemetryId ||
        this.telemetryId === "anonymous" ||
        this.telemetryId === "anonymous-supabase"
      ) {
        this.telemetryId = (await getOrCreateMem0UserId()) || "anonymous";
      }
      return this.telemetryId;
    } catch {
      this.telemetryId = "anonymous";
      return this.telemetryId;
    }
  }

  static fromConfig(configDict: Record<string, any>): Memory {
    try {
      const config = MemoryConfigSchema.parse(configDict);
      return new Memory(config);
    } catch (e) {
      console.error("Configuration validation error:", e);
      throw e;
    }
  }

  async updateProject(options: UpdateProjectOptions = {}): Promise<never> {
    if (options?.decay === true) {
      await this._getNoticeTelemetryId();
      throw new Error(await getDecayFeatureErrorMessage(this));
    }

    throw new Error("Project updates are not supported by the OSS Memory SDK.");
  }

  private recallMode(): "stateAware" | "legacy" {
    return this.config.recall?.mode ?? "stateAware";
  }

  private async persistVerifiedBatch(
    vectors: number[][],
    ids: string[],
    payloads: Record<string, any>[],
  ): Promise<void> {
    try {
      await this.vectorStore.insert(vectors, ids, payloads);
      const persisted = await Promise.all(
        ids.map((id) => this.vectorStore.get(id)),
      );
      if (persisted.some((row) => row === null)) {
        throw new MemoryPersistenceError(
          "Memory provider silently persisted only part of the planned batch",
          { plannedIds: ids },
        );
      }
    } catch (cause) {
      await Promise.allSettled(ids.map((id) => this.vectorStore.delete(id)));
      if (cause instanceof MemoryPersistenceError) throw cause;
      throw new MemoryPersistenceError(
        "Memory batch persistence failed; all planned IDs were rolled back",
        { plannedIds: ids, cause },
      );
    }
  }

  private async replacePayload(
    memoryId: string,
    payload: Record<string, any>,
  ): Promise<void> {
    const text = payload.data;
    if (typeof text !== "string" || !text) {
      throw new MemoryPersistenceError(
        `Memory ${memoryId} cannot be updated because its stored content is missing`,
        { plannedIds: [memoryId] },
      );
    }
    const vector = await this.embedder.embed(text, "update");
    await this.vectorStore.update(memoryId, vector, payload);
  }

  private payloadMatchesFilters(
    payload: Record<string, any>,
    filters: SearchFilters,
  ): boolean {
    return Object.entries(filters).every(([key, expected]) => {
      if (
        key.startsWith("$") ||
        (expected !== null && typeof expected === "object")
      ) {
        return false;
      }
      return expected === "*" ? key in payload : payload[key] === expected;
    });
  }

  private async lifecycleCandidates(
    filters: SearchFilters,
    limit = 1000,
  ): Promise<Array<{ id: string; payload: Record<string, any> }>> {
    if (this.vectorStore.supportsCompleteScopedList !== true) {
      throw new MemoryPersistenceError(
        "State transition candidate discovery requires provider complete-list capability",
      );
    }
    let rows: Array<{ id: string; payload: Record<string, any> }>;
    let total: number;
    try {
      [rows, total] = await this.completeScopeList(filters, limit);
    } catch (cause) {
      throw new MemoryPersistenceError(
        "State transition candidate discovery failed",
        { cause },
      );
    }
    if (
      !Array.isArray(rows) ||
      typeof total !== "number" ||
      total !== rows.length
    ) {
      throw new MemoryPersistenceError(
        "State transition candidate discovery was incomplete",
      );
    }
    if (rows.length >= limit) {
      throw new MemoryPersistenceError(
        "State transition candidate discovery reached its safety limit",
      );
    }
    if (rows.some((row) => !this.payloadMatchesFilters(row.payload, filters))) {
      throw new MemoryPersistenceError(
        "State transition candidate discovery escaped the identity scope",
      );
    }
    return rows
      .filter(
        (row) =>
          row.payload?._mem0_schema_version === schemaVersion &&
          row.payload?._mem0_state_key &&
          stateLikeKinds.has(row.payload?._mem0_kind) &&
          ["active", "disputed", "superseded"].includes(
            row.payload?._mem0_status ?? "active",
          ),
      )
      .map((row) => ({ id: String(row.id), payload: { ...row.payload } }))
      .sort((left, right) => left.id.localeCompare(right.id));
  }

  private async completeScopeList(
    filters: SearchFilters,
    limit: number,
  ): Promise<[VectorStoreResult[], number]> {
    if (this.vectorStore.supportsCompleteScopedList !== true) {
      throw new MemoryPersistenceError(
        "Provider complete-list capability is required",
      );
    }
    if (typeof this.vectorStore.listComplete === "function") {
      return this.vectorStore.listComplete(filters, limit);
    }
    return this.vectorStore.list(filters, limit);
  }

  async add(
    messages: string | Message[],
    config: AddMemoryOptions,
  ): Promise<SearchResult> {
    if (config?.timestamp !== undefined) {
      await this._getNoticeTelemetryId();
      throw new Error(
        await getTemporalFeatureErrorMessage(this, {
          triggerFunction: "add",
          triggerParameter: "timestamp",
        }),
      );
    }

    // Validate messages input
    if (messages === undefined || messages === null) {
      throw new Error(
        "messages is required and cannot be undefined or null. Provide a string or array of messages.",
      );
    }
    if (Array.isArray(messages)) {
      if (messages.length === 0) {
        throw new Error(
          "messages array cannot be empty. Provide at least one message with non-empty content.",
        );
      }
      const allBlank = messages.every(
        (m) => typeof m.content === "string" && m.content.trim() === "",
      );
      if (allBlank) {
        throw new Error(
          "messages array cannot contain only blank content. Provide at least one message with non-empty content.",
        );
      }
    } else if (messages.trim() === "") {
      throw new Error(
        "messages string cannot be empty. Provide non-empty content.",
      );
    }

    const temporalUsageNotice = detectTemporalUsageFromMetadata(
      config?.metadata,
    );

    await this._ensureInitialized();
    await this._captureEvent("add", {
      message_count: Array.isArray(messages) ? messages.length : 1,
      has_metadata: !!config.metadata,
      has_filters: !!config.filters,
      infer: config.infer,
    });
    const {
      metadata: callerMetadata = {},
      filters = {},
      infer = true,
    } = config;
    const metadata = stripInternalMetadata(callerMetadata);

    // Validate and trim entity IDs
    const userId = validateAndTrimEntityId(config.userId, "userId");
    const agentId = validateAndTrimEntityId(config.agentId, "agentId");
    const runId = validateAndTrimEntityId(config.runId, "runId");

    // Convert camelCase entity params to snake_case for storage (matches API and search/getAll filters)
    if (userId) filters.user_id = metadata.user_id = userId;
    if (agentId) filters.agent_id = metadata.agent_id = agentId;
    if (runId) filters.run_id = metadata.run_id = runId;

    // Normalize expiration date into the stored metadata (round-trips via get()).
    if (config.expirationDate != null) {
      metadata.expiration_date = normalizeExpirationDate(config.expirationDate);
    }

    if (!filters.user_id && !filters.agent_id && !filters.run_id) {
      throw new Error(
        "One of the filters: userId, agentId or runId is required!",
      );
    }

    const parsedMessages = Array.isArray(messages)
      ? (messages as Message[])
      : [{ role: "user", content: messages }];

    const final_parsedMessages = await parse_vision_messages(parsedMessages);

    // Add to vector store
    const vectorStoreResult = await this.addToVectorStore(
      final_parsedMessages,
      metadata,
      filters,
      infer,
    );

    if (temporalUsageNotice) {
      await this._displayTemporalUsageNotice({
        triggerFunction: "add",
        triggerSource: temporalUsageNotice.triggerSource,
        triggerReason: temporalUsageNotice.triggerReason,
      });
    } else {
      const scaleThresholdNotice = await detectScaleThresholdFromAddResult(
        this,
        vectorStoreResult,
      );
      if (scaleThresholdNotice) {
        await this._displayScaleThresholdNotice({
          triggerFunction: "add",
          ...scaleThresholdNotice,
        });
      } else {
        await this._displayFirstRunNotice("add");
      }
    }

    return {
      results: vectorStoreResult,
    };
  }

  private async addToVectorStore(
    messages: Message[],
    metadata: Record<string, any>,
    filters: SearchFilters,
    infer: boolean,
  ): Promise<MemoryItem[]> {
    if (!infer) {
      if (this.recallMode() === "stateAware") {
        const eligible = messages
          .map((message, messageIndex) => ({ message, messageIndex }))
          .filter(
            ({ message }) =>
              message.role !== "system" &&
              typeof message.content === "string" &&
              message.content.length > 0,
          );
        const texts = eligible.map(({ message }) => message.content as string);
        if (texts.length === 0) return [];
        let vectors: number[][];
        try {
          vectors = await this.embedder.embedBatch(texts, "add");
        } catch (cause) {
          throw new MemoryPersistenceError(
            "Failed to produce a complete embedding batch before persistence",
            { cause },
          );
        }
        if (
          vectors.length !== texts.length ||
          vectors.some((vector) => !Array.isArray(vector))
        ) {
          throw new MemoryPersistenceError(
            "Failed to produce a complete embedding batch before persistence",
          );
        }

        const ids = eligible.map(() => uuidv4());
        const payloads = eligible.map(({ message, messageIndex }, index) => {
          const text = texts[index];
          const now = new Date().toISOString();
          return {
            ...metadata,
            ...buildLifecycleMetadata(
              {
                kind: "event",
                value: text,
                evidence: { message_index: messageIndex, excerpt: text },
              },
              messages,
            ),
            data: text,
            hash: createHash("md5").update(text).digest("hex"),
            textLemmatized: lemmatizeForBm25(text),
            createdAt: now,
            updatedAt: now,
          };
        });
        await this.persistVerifiedBatch(vectors, ids, payloads);
        try {
          await Promise.all(
            ids.map((id, index) =>
              this.db.addHistory(
                id,
                null,
                texts[index],
                "ADD",
                payloads[index].createdAt,
              ),
            ),
          );
        } catch (cause) {
          await Promise.allSettled(
            ids.map((id) => this.vectorStore.delete(id)),
          );
          throw new MemoryPersistenceError(
            "History persistence failed; vector writes were rolled back",
            { plannedIds: ids, cause },
          );
        }
        return ids.map((id, index) => ({
          id,
          memory: texts[index],
          metadata: { event: "ADD" },
        }));
      }
      const returnedMemories: MemoryItem[] = [];
      for (const message of messages) {
        if (message.role === "system") {
          continue;
        }
        const memoryId = await this.createMemory(
          message.content as string,
          {},
          metadata,
        );
        returnedMemories.push({
          id: memoryId,
          memory: message.content as string,
          metadata: { event: "ADD" },
        });
      }
      return returnedMemories;
    }

    // === V3 PHASED BATCH PIPELINE ===

    // Phase 0: Context gathering
    const sessionScope = this.buildSessionScope(filters);
    let lastMessages: Array<{
      role: string;
      content: string;
      name?: string;
    }> = [];
    if (typeof this.db.getLastMessages === "function") {
      try {
        lastMessages = await this.db.getLastMessages(sessionScope, 10);
      } catch {
        // getLastMessages not supported — proceed without context
      }
    }
    if (this.recallMode() === "stateAware") {
      lastMessages = lastMessages.filter(
        (message) =>
          message.role !== "system" && typeof message.content === "string",
      );
    }
    // Preserve role on the messages being extracted so the prompt's role-aware
    // logic and the required `attributed_to` output have the speaker to work
    // with. Matches the Python oss `parse_messages` helper (`role: content`);
    // without this, assistant statements get attributed to the user.
    const eligibleMessages = messages.filter(
      (message) =>
        message.role !== "system" && typeof message.content === "string",
    );
    const parsedMessages =
      this.recallMode() === "stateAware"
        ? JSON.stringify(
            messages
              .map((message, messageIndex) => ({
                message_index: messageIndex,
                role: message.role,
                content: message.content,
              }))
              .filter(
                (message) =>
                  message.role !== "system" &&
                  typeof message.content === "string",
              ),
          )
        : messages.map((m) => `${m.role}: ${m.content}`).join("\n");

    // Phase 1: Existing memory retrieval
    const queryEmbedding = await this.embedder.embed(parsedMessages, "search");
    const existingResults = await this.vectorStore.search(
      queryEmbedding,
      10,
      filters,
    );

    // Map UUIDs to integers (anti-hallucination)
    const existingMemories: Array<{ id: string; text: string }> = [];
    const uuidMapping: Record<string, string> = {};
    for (let idx = 0; idx < existingResults.length; idx++) {
      const mem = existingResults[idx];
      uuidMapping[String(idx)] = mem.id;
      existingMemories.push({
        id: String(idx),
        text: mem.payload?.data ?? "",
      });
    }

    // Phase 2: LLM extraction (single call)
    const isAgentScoped = !!filters.agent_id && !filters.user_id;
    let systemPrompt = ADDITIVE_EXTRACTION_PROMPT;
    if (isAgentScoped) {
      systemPrompt += AGENT_CONTEXT_SUFFIX;
    }

    const userPrompt = generateAdditiveExtractionPrompt({
      existingMemories,
      newMessages: parsedMessages,
      lastKMessages: lastMessages,
      customInstructions: this.customInstructions,
    });

    let response: string;
    try {
      response = (await this.llm.generateResponse(
        [
          { role: "system", content: systemPrompt },
          { role: "user", content: userPrompt },
        ],
        { type: "json_object" },
      )) as string;
    } catch (e) {
      console.error("LLM extraction failed:", e);
      throw new LLMError(`LLM extraction failed: ${e}`, { cause: e });
    }

    // Parse response
    let extractedMemories: Array<{
      id?: string;
      text?: string;
      attributed_to?: string;
      linked_memory_ids?: string[];
      kind?: string;
      subject?: string;
      predicate?: string;
      value?: unknown;
      evidence?: { message_index: number; excerpt: string };
      evidence_strength?: number;
      relation?: string;
      valid_from?: string;
      valid_to?: string;
      _mem0Lifecycle?: Record<string, any>;
    }> = [];
    try {
      const cleanResponse = extractJson(response);
      if (cleanResponse && cleanResponse.trim()) {
        try {
          const parsed = AdditiveExtractionSchema.parse(
            JSON.parse(cleanResponse),
          );
          extractedMemories = parsed.memory;
        } catch {
          const fallbackJson = extractJson(cleanResponse);
          extractedMemories = JSON.parse(fallbackJson)?.memory ?? [];
        }
      }
    } catch (e) {
      console.error("Error parsing extraction response:", e);
      extractedMemories = [];
    }

    if (this.recallMode() === "stateAware") {
      extractedMemories = extractedMemories.flatMap((memory) => {
        try {
          const lifecycle = buildLifecycleMetadata(memory, messages);
          lifecycle._mem0_related_to = (lifecycle._mem0_related_to ?? [])
            .map((linkedId: string) => uuidMapping[linkedId])
            .filter(Boolean);
          return [{ ...memory, _mem0Lifecycle: lifecycle }];
        } catch (error) {
          console.warn(`Skipping ungrounded memory extraction: ${error}`);
          return [];
        }
      });
    }

    if (extractedMemories.length === 0) {
      // Save messages even if nothing extracted
      if (typeof this.db.saveMessages === "function") {
        try {
          await this.db.saveMessages(
            eligibleMessages.map((m) => ({
              role: m.role,
              content: m.content as string,
            })),
            sessionScope,
          );
        } catch {}
      }
      return [];
    }

    let lifecycleCandidates: Array<{
      id: string;
      payload: Record<string, any>;
    }> = [];
    if (this.recallMode() === "stateAware") {
      const stateMemories = extractedMemories.filter((memory) =>
        stateLikeKinds.has(memory._mem0Lifecycle?._mem0_kind),
      );
      if (
        stateMemories.some((memory) => !memory._mem0Lifecycle?._mem0_state_key)
      ) {
        throw new MemoryPersistenceError(
          "State-like extraction requires a normalized state key",
        );
      }
      if (stateMemories.length > 0) {
        lifecycleCandidates = await this.lifecycleCandidates(filters);
        for (const memory of stateMemories) {
          const lifecycle = memory._mem0Lifecycle!;
          if (
            !["supersedes", "corroborates", "conflicts_with"].includes(
              lifecycle._mem0_relation,
            )
          ) {
            continue;
          }
          const related = new Set<string>(lifecycle._mem0_related_to ?? []);
          const linkedKeys = new Set(
            lifecycleCandidates
              .filter(
                (candidate) =>
                  related.has(candidate.id) &&
                  candidate.payload._mem0_kind === lifecycle._mem0_kind,
              )
              .map((candidate) => candidate.payload._mem0_state_key)
              .filter(Boolean),
          );
          if (linkedKeys.size > 1) {
            throw new MemoryPersistenceError(
              "Linked state transition spans multiple canonical state keys",
            );
          }
          if (linkedKeys.size === 1) {
            lifecycle._mem0_state_key = [...linkedKeys][0];
          }
        }
      }
    }

    // Phase 3: Batch embed all extracted memory texts
    const memTexts = extractedMemories
      .map((m) => m.text ?? "")
      .filter((t) => t.length > 0);
    let embedMap: Record<string, number[]> = {};
    try {
      const memEmbeddingsList = await this.embedder.embedBatch(memTexts, "add");
      if (
        memEmbeddingsList.length !== memTexts.length ||
        memEmbeddingsList.some((vector) => !Array.isArray(vector))
      ) {
        throw new Error(
          `embedBatch returned ${memEmbeddingsList.length} vectors for ${memTexts.length} texts`,
        );
      }
      for (let i = 0; i < memTexts.length; i++) {
        embedMap[memTexts[i]] = memEmbeddingsList[i];
      }
    } catch (cause) {
      if (this.recallMode() === "stateAware") {
        throw new MemoryPersistenceError(
          "Failed to produce a complete embedding batch before persistence",
          { cause },
        );
      }
      // Legacy mode preserves the former best-effort individual fallback.
      for (const text of memTexts) {
        try {
          embedMap[text] = await this.embedder.embed(text, "add");
        } catch (e) {
          console.warn(`Failed to embed memory text: ${e}`);
        }
      }
    }

    // Phase 4-5: CPU processing + hash dedup
    const existingHashes = new Set<string>();
    for (const mem of existingResults) {
      const h = mem.payload?.hash;
      if (h) existingHashes.add(h);
    }

    const records: Array<{
      memoryId: string;
      text: string;
      embedding: number[];
      payload: Record<string, any>;
    }> = [];
    const seenHashes = new Set<string>();
    const predecessorUpdates: Array<{
      memoryId: string;
      oldPayload: Record<string, any>;
      newPayload: Record<string, any>;
    }> = [];
    const corroboratedResults: MemoryItem[] = [];

    for (const mem of extractedMemories) {
      const text = mem.text;
      if (!text || !(text in embedMap)) continue;

      const lifecycle = { ...(mem._mem0Lifecycle ?? {}) };
      const stateKey = lifecycle._mem0_state_key;
      const sameState = stateKey
        ? lifecycleCandidates.filter(
            (candidate) =>
              candidate.payload._mem0_state_key === stateKey &&
              ["active", "disputed"].includes(
                candidate.payload._mem0_status ?? "active",
              ),
          )
        : [];
      const canonical = sameState.find(
        (candidate) => candidate.payload._mem0_value === lifecycle._mem0_value,
      );
      if (
        this.recallMode() === "stateAware" &&
        stateLikeKinds.has(lifecycle._mem0_kind) &&
        canonical
      ) {
        const corroboratedPayload = { ...canonical.payload };
        corroboratedPayload._mem0_source_hashes = [
          ...new Set([
            ...(corroboratedPayload._mem0_source_hashes ?? []),
            ...(lifecycle._mem0_source_hashes ?? []),
          ]),
        ].sort();
        corroboratedPayload._mem0_source_message_indexes = [
          ...new Set([
            ...(corroboratedPayload._mem0_source_message_indexes ?? []),
            ...(lifecycle._mem0_source_message_indexes ?? []),
          ]),
        ].sort((left, right) => left - right);
        corroboratedPayload._mem0_evidence_strength = Math.min(
          1,
          Number(corroboratedPayload._mem0_evidence_strength ?? 0.5) +
            0.1 * Number(lifecycle._mem0_evidence_strength ?? 1),
        );
        predecessorUpdates.push({
          memoryId: canonical.id,
          oldPayload: { ...canonical.payload },
          newPayload: corroboratedPayload,
        });
        canonical.payload = { ...corroboratedPayload };
        corroboratedResults.push({
          id: canonical.id,
          memory: String(canonical.payload.data ?? text),
          metadata: { event: "NONE" },
        });
        continue;
      }

      const memHash = createHash("md5").update(text).digest("hex");
      const isEvent = lifecycle._mem0_kind === "event";
      if (
        !isEvent &&
        (existingHashes.has(memHash) || seenHashes.has(memHash))
      ) {
        continue;
      }
      if (!isEvent) seenHashes.add(memHash);

      const textLemmatized = lemmatizeForBm25(text);
      const memoryId = uuidv4();
      const now = new Date().toISOString();

      const memPayload: Record<string, any> = {
        ...metadata,
        ...lifecycle,
        data: text,
        textLemmatized,
        hash: memHash,
        createdAt: now,
        updatedAt: now,
      };
      if (mem.attributed_to) {
        memPayload.attributedTo = mem.attributed_to;
      }
      if (filters.user_id) memPayload.user_id = filters.user_id;
      if (filters.agent_id) memPayload.agent_id = filters.agent_id;
      if (filters.run_id) memPayload.run_id = filters.run_id;

      if (stateKey && memPayload._mem0_valid_from === undefined) {
        memPayload._mem0_valid_from = now;
      }
      if (
        this.recallMode() === "stateAware" &&
        stateLikeKinds.has(lifecycle._mem0_kind) &&
        lifecycle._mem0_relation === "supersedes" &&
        sameState.length > 0
      ) {
        const supersedingValidFrom = memPayload._mem0_valid_from ?? now;
        memPayload._mem0_supersedes = sameState
          .map((candidate) => candidate.id)
          .sort();
        for (const candidate of sameState) {
          const updated = {
            ...candidate.payload,
            _mem0_status: "superseded",
            _mem0_valid_to: supersedingValidFrom,
            _mem0_superseded_by: memoryId,
          };
          predecessorUpdates.push({
            memoryId: candidate.id,
            oldPayload: { ...candidate.payload },
            newPayload: updated,
          });
          candidate.payload = { ...updated };
        }
      } else if (
        this.recallMode() === "stateAware" &&
        stateLikeKinds.has(lifecycle._mem0_kind) &&
        sameState.length > 0
      ) {
        memPayload._mem0_status = "disputed";
        memPayload._mem0_conflicts_with = sameState
          .map((candidate) => candidate.id)
          .sort();
        for (const candidate of sameState) {
          const updated = {
            ...candidate.payload,
            _mem0_status: "disputed",
            _mem0_conflicts_with: [
              ...new Set([
                ...(candidate.payload._mem0_conflicts_with ?? []),
                memoryId,
              ]),
            ].sort(),
          };
          predecessorUpdates.push({
            memoryId: candidate.id,
            oldPayload: { ...candidate.payload },
            newPayload: updated,
          });
          candidate.payload = { ...updated };
        }
      }

      records.push({
        memoryId,
        text,
        embedding: embedMap[text],
        payload: memPayload,
      });
      if (stateKey) {
        lifecycleCandidates.push({ id: memoryId, payload: { ...memPayload } });
      }
    }

    if (records.length === 0) {
      const applied: typeof predecessorUpdates = [];
      try {
        for (const update of predecessorUpdates) {
          await this.replacePayload(update.memoryId, update.newPayload);
          applied.push(update);
        }
      } catch (cause) {
        await Promise.allSettled(
          applied
            .reverse()
            .map((update) =>
              this.replacePayload(update.memoryId, update.oldPayload),
            ),
        );
        throw new MemoryPersistenceError(
          "Memory corroboration persistence failed; prior payloads were restored",
          {
            plannedIds: predecessorUpdates.map((update) => update.memoryId),
            cause,
          },
        );
      }
      if (typeof this.db.saveMessages === "function") {
        try {
          await this.db.saveMessages(
            eligibleMessages.map((m) => ({
              role: m.role,
              content: m.content as string,
            })),
            sessionScope,
          );
        } catch {}
      }
      return corroboratedResults;
    }

    // Phase 6: Batch persist
    const allVectors = records.map((r) => r.embedding);
    const allIds = records.map((r) => r.memoryId);
    const allPayloads = records.map((r) => r.payload);

    if (this.recallMode() === "stateAware") {
      await this.persistVerifiedBatch(allVectors, allIds, allPayloads);
      const applied: typeof predecessorUpdates = [];
      try {
        for (const update of predecessorUpdates) {
          await this.replacePayload(update.memoryId, update.newPayload);
          applied.push(update);
        }
      } catch (cause) {
        await Promise.allSettled(
          allIds.map((id) => this.vectorStore.delete(id)),
        );
        await Promise.allSettled(
          applied
            .reverse()
            .map((update) =>
              this.replacePayload(update.memoryId, update.oldPayload),
            ),
        );
        throw new MemoryPersistenceError(
          "State transition persistence failed; new memories were rolled back",
          { plannedIds: allIds, cause },
        );
      }
    } else {
      try {
        await this.vectorStore.insert(allVectors, allIds, allPayloads);
      } catch {
        // Legacy mode preserves the former best-effort individual fallback.
        for (let i = 0; i < allIds.length; i++) {
          try {
            await this.vectorStore.insert(
              [allVectors[i]],
              [allIds[i]],
              [allPayloads[i]],
            );
          } catch (e) {
            console.error(`Failed to insert memory ${allIds[i]}: ${e}`);
          }
        }
      }
    }

    // Batch history
    const historyRecords = records.map((r) => ({
      memoryId: r.memoryId,
      previousValue: null as string | null,
      newValue: r.text as string | null,
      action: "ADD",
      createdAt: r.payload.createdAt as string | undefined,
      updatedAt: undefined as string | undefined,
      isDeleted: 0,
    }));

    if (this.recallMode() === "stateAware") {
      try {
        if (typeof this.db.batchAddHistory === "function") {
          await this.db.batchAddHistory(historyRecords);
        } else {
          for (const hr of historyRecords) {
            await this.db.addHistory(
              hr.memoryId,
              null,
              hr.newValue,
              "ADD",
              hr.createdAt,
            );
          }
        }
      } catch (cause) {
        await Promise.allSettled(
          allIds.map((id) => this.vectorStore.delete(id)),
        );
        await Promise.allSettled(
          predecessorUpdates.map((update) =>
            this.replacePayload(update.memoryId, update.oldPayload),
          ),
        );
        throw new MemoryPersistenceError(
          "History persistence failed; vector writes were rolled back",
          { plannedIds: allIds, cause },
        );
      }
    } else if (typeof this.db.batchAddHistory === "function") {
      try {
        await this.db.batchAddHistory(historyRecords);
      } catch {
        // Fallback: add one by one
        for (const hr of historyRecords) {
          try {
            await this.db.addHistory(
              hr.memoryId,
              null,
              hr.newValue,
              "ADD",
              hr.createdAt,
            );
          } catch (e) {
            console.error(`Failed to add history for ${hr.memoryId}: ${e}`);
          }
        }
      }
    } else {
      for (const hr of historyRecords) {
        try {
          await this.db.addHistory(
            hr.memoryId,
            null,
            hr.newValue,
            "ADD",
            hr.createdAt,
          );
        } catch (e) {
          console.error(`Failed to add history for ${hr.memoryId}: ${e}`);
        }
      }
    }

    // Phase 7: Batch entity linking
    try {
      const allTexts = records.map((r) => r.text);
      const allEntities = extractEntitiesBatch(allTexts);

      // 7a: Global dedup — collect unique entities across all memories
      const globalEntities: Record<
        string,
        { entityType: string; entityText: string; memoryIds: Set<string> }
      > = {};
      for (let idx = 0; idx < records.length; idx++) {
        const memoryId = records[idx].memoryId;
        const entities = idx < allEntities.length ? allEntities[idx] : [];
        for (const entity of entities) {
          const key = entity.text.trim().toLowerCase();
          if (key in globalEntities) {
            globalEntities[key].memoryIds.add(memoryId);
          } else {
            globalEntities[key] = {
              entityType: entity.type,
              entityText: entity.text,
              memoryIds: new Set([memoryId]),
            };
          }
        }
      }

      const orderedKeys = Object.keys(globalEntities);
      if (orderedKeys.length > 0) {
        const entityTexts = orderedKeys.map(
          (k) => globalEntities[k].entityText,
        );

        // 7b: Single batch embed for all unique entities
        let entityEmbeddings: (number[] | null)[];
        try {
          entityEmbeddings = await this.embedder.embedBatch(entityTexts, "add");
        } catch {
          // Fallback: embed individually
          entityEmbeddings = [];
          for (const t of entityTexts) {
            try {
              entityEmbeddings.push(await this.embedder.embed(t, "add"));
            } catch {
              entityEmbeddings.push(null);
            }
          }
        }

        // Filter out entities with failed embeddings
        const valid: Array<{ index: number; key: string }> = [];
        for (let i = 0; i < orderedKeys.length; i++) {
          if (entityEmbeddings[i] !== null) {
            valid.push({ index: i, key: orderedKeys[i] });
          }
        }

        if (valid.length > 0) {
          const entityStore = await this.getEntityStore();
          const exactMatches = await this._existingEntitiesByText(
            entityStore,
            filters,
          );

          // 7c: Search for existing entities one by one (no batch search)
          const toInsertVectors: number[][] = [];
          const toInsertIds: string[] = [];
          const toInsertPayloads: Record<string, any>[] = [];

          for (const { index: j, key } of valid) {
            const { entityType, entityText, memoryIds } = globalEntities[key];
            const entityVec = entityEmbeddings[j]!;

            let matches: Array<{
              id: string;
              score?: number;
              payload: Record<string, any>;
            }> = [];
            const exactMatch = exactMatches.get(key);
            if (!exactMatch) {
              try {
                matches = await entityStore.search(entityVec, 1, filters);
              } catch {}
            }

            const semanticMatch =
              matches.length > 0 && (matches[0].score ?? 0) >= 0.95
                ? matches[0]
                : undefined;
            const match = exactMatch ?? semanticMatch;
            if (match) {
              // Update existing entity
              const payload = match.payload || {};
              const linked = new Set<string>(payload.linkedMemoryIds ?? []);
              for (const mid of memoryIds) linked.add(mid);
              payload.linkedMemoryIds = Array.from(linked).sort();
              try {
                await entityStore.update(match.id, entityVec, payload);
              } catch (e) {
                console.debug(`Entity update failed for '${entityText}': ${e}`);
              }
            } else {
              // New entity — collect for batch insert
              const entityPayload: Record<string, any> = {
                data: entityText,
                entityType,
                linkedMemoryIds: Array.from(memoryIds).sort(),
              };
              if (filters.user_id) entityPayload.user_id = filters.user_id;
              if (filters.agent_id) entityPayload.agent_id = filters.agent_id;
              if (filters.run_id) entityPayload.run_id = filters.run_id;

              toInsertVectors.push(entityVec);
              toInsertIds.push(uuidv4());
              toInsertPayloads.push(entityPayload);
            }
          }

          // 7e: Single batch insert for all new entities
          if (toInsertVectors.length > 0) {
            try {
              await entityStore.insert(
                toInsertVectors,
                toInsertIds,
                toInsertPayloads,
              );
            } catch (e) {
              console.warn(`Batch entity insert failed: ${e}`);
            }
          }
        }
      }
    } catch (e) {
      console.warn(`Batch entity linking failed: ${e}`);
    }

    // Phase 8: Save messages + return
    if (typeof this.db.saveMessages === "function") {
      try {
        await this.db.saveMessages(
          eligibleMessages.map((m) => ({
            role: m.role,
            content: m.content as string,
          })),
          sessionScope,
        );
      } catch {}
    }

    return records.map((r) => ({
      id: r.memoryId,
      memory: r.text,
      metadata: { event: "ADD" },
    }));
  }

  async get(memoryId: string): Promise<MemoryItem | null> {
    await this._ensureInitialized();
    const memory = await this.vectorStore.get(memoryId);
    if (!memory) {
      await this._displayFirstRunNotice("get");
      return null;
    }

    const filters = {
      ...(memory.payload.user_id && { user_id: memory.payload.user_id }),
      ...(memory.payload.agent_id && { agent_id: memory.payload.agent_id }),
      ...(memory.payload.run_id && { run_id: memory.payload.run_id }),
    };

    const memoryItem: MemoryItem = {
      id: memory.id,
      memory: memory.payload.data,
      hash: memory.payload.hash,
      createdAt: memory.payload.createdAt,
      updatedAt: memory.payload.updatedAt,
      metadata: {},
    };

    // Add additional metadata
    const excludedKeys = new Set([
      "userId",
      "agentId",
      "runId",
      "hash",
      "data",
      "createdAt",
      "updatedAt",
      "textLemmatized",
      "attributedTo",
    ]);
    for (const [key, value] of Object.entries(memory.payload)) {
      if (!excludedKeys.has(key) && !key.startsWith(internalPrefix)) {
        memoryItem.metadata![key] = value;
      }
    }

    const result = {
      ...memoryItem,
      ...filters,
      ...(memory.payload.attributedTo && {
        attributedTo: memory.payload.attributedTo,
      }),
    };
    await this._displayFirstRunNotice("get");
    return result;
  }

  async search(
    query: string,
    config: SearchMemoryOptions,
  ): Promise<SearchResult> {
    if (config?.referenceDate !== undefined) {
      await this._getNoticeTelemetryId();
      throw new Error(
        await getTemporalFeatureErrorMessage(this, {
          triggerFunction: "search",
          triggerParameter: "referenceDate",
        }),
      );
    }

    const temporalUsageNotice = detectTemporalUsageFromSearch(
      query,
      config?.filters,
    );

    // Reject top-level entity params - must use filters instead
    rejectTopLevelEntityParams(config as Record<string, any>, "search");

    // Validate search parameters (before applying defaults)
    validateSearchParams(config.threshold, config.topK, config.maxChars);

    // Validate and trim entity IDs in filters. Only include keys whose
    // validated value is defined — otherwise downstream vector stores
    // receive `agent_id: undefined` / `run_id: undefined` and fail
    // (Qdrant rejects the malformed match, pgvector binds NULL, Redis
    // emits a literal "undefined" string in TAG filters).
    const normalizedFilters: Record<string, any> = config.filters
      ? Object.fromEntries(
          Object.entries({
            ...config.filters,
            user_id: validateAndTrimEntityId(config.filters.user_id, "user_id"),
            agent_id: validateAndTrimEntityId(
              config.filters.agent_id,
              "agent_id",
            ),
            run_id: validateAndTrimEntityId(config.filters.run_id, "run_id"),
          }).filter(([, v]) => v !== undefined),
        )
      : {};

    await this._ensureInitialized();
    const {
      topK = 20,
      threshold = 0.1,
      explain = false,
      showExpired = false,
    } = config;

    await this._captureEvent("search", {
      query_length: query.length,
      topK,
      has_filters: !!config.filters,
    });

    let effectiveFilters: Record<string, any> = { ...normalizedFilters };

    // Apply enhanced metadata filtering if advanced operators are detected
    if (this._hasAdvancedOperators(effectiveFilters)) {
      const processedFilters = this._processMetadataFilters(effectiveFilters);
      // Remove logical/operator keys that have been reprocessed
      for (const logicalKey of ["AND", "OR", "NOT"]) {
        delete effectiveFilters[logicalKey];
      }
      for (const fk of Object.keys(effectiveFilters)) {
        if (
          !["AND", "OR", "NOT", "user_id", "agent_id", "run_id"].includes(fk) &&
          typeof effectiveFilters[fk] === "object" &&
          effectiveFilters[fk] !== null
        ) {
          delete effectiveFilters[fk];
        }
      }
      effectiveFilters = { ...effectiveFilters, ...processedFilters };
    }

    // Validate filters contains at least one entity ID (snake_case)
    if (
      !effectiveFilters.user_id &&
      !effectiveFilters.agent_id &&
      !effectiveFilters.run_id
    ) {
      throw new Error(
        "filters must contain at least one of: user_id, agent_id, run_id. " +
          "Example: filters: { user_id: 'u1' }",
      );
    }

    const searchStartMs = Date.now();

    // Step 1: Preprocess query
    const queryLemmatized = lemmatizeForBm25(query);
    const queryEntities = extractEntities(query);

    // Step 2: Embed query
    const queryEmbedding = await this.embedder.embed(query, "search");

    // Step 3: Semantic search (over-fetch for scoring pool)
    const internalLimit = Math.max(topK * 4, 60);
    const semanticResults = await this.vectorStore.search(
      queryEmbedding,
      internalLimit,
      effectiveFilters,
    );

    // Step 4: Keyword search (if store supports it)
    let keywordResults: Array<{
      id: string;
      score?: number;
      payload: Record<string, any>;
    }> | null = null;
    if (typeof this.vectorStore.keywordSearch === "function") {
      try {
        keywordResults =
          (await this.vectorStore.keywordSearch(
            queryLemmatized,
            internalLimit,
            effectiveFilters,
          )) ?? null;
      } catch {
        keywordResults = null;
      }
    }

    // Step 5: Compute BM25 scores from keyword results
    const bm25Scores: Record<string, number> = {};
    if (keywordResults) {
      const [midpoint, steepness] = getBm25Params(query, queryLemmatized);
      for (const mem of keywordResults) {
        const memId = String(mem.id);
        const rawScore = mem.score ?? 0;
        if (rawScore > 0) {
          bm25Scores[memId] = normalizeBm25(rawScore, midpoint, steepness);
        }
      }
    }

    // Step 6: Compute entity boosts
    const entityBoosts: Record<string, number> = {};
    const entityMemoryIds = new Set<string>();
    if (queryEntities.length > 0) {
      try {
        // Deduplicate entities (max 8)
        const seen = new Set<string>();
        const deduped: Array<{ type: string; text: string }> = [];
        for (const entity of queryEntities.slice(0, 8)) {
          const key = entity.text.trim().toLowerCase();
          if (key && !seen.has(key)) {
            seen.add(key);
            deduped.push(entity);
          }
        }

        if (deduped.length > 0) {
          const entityStore = await this.getEntityStore();
          const entitySearchFilters: Record<string, any> = {};
          for (const k of ["user_id", "agent_id", "run_id"] as const) {
            if (effectiveFilters[k])
              entitySearchFilters[k] = effectiveFilters[k];
          }
          const entityTexts = deduped.map((e) => e.text);
          const embeddings = await this.embedder.embedBatch(
            entityTexts,
            "search",
          );

          if (embeddings.length !== entityTexts.length) {
            console.warn(
              `embedBatch returned ${embeddings.length} vectors for ${entityTexts.length} texts — skipping entity boost`,
            );
          } else {
            const searchResults = await Promise.allSettled(
              deduped.map((_, i) =>
                entityStore.search(embeddings[i], 500, entitySearchFilters),
              ),
            );

            for (const result of searchResults) {
              if (result.status === "rejected") {
                console.warn(
                  "Entity boost search failed for one entity:",
                  result.reason,
                );
                continue;
              }

              for (const match of result.value) {
                const similarity = match.score ?? 0;
                if (similarity < 0.5) continue;

                const payload = match.payload || {};
                const linkedMemoryIds = payload.linkedMemoryIds ?? [];
                if (!Array.isArray(linkedMemoryIds)) continue;

                const numLinked = Math.max(linkedMemoryIds.length, 1);
                const memoryCountWeight =
                  1.0 / (1.0 + 0.001 * (numLinked - 1) ** 2);
                const boost =
                  similarity * ENTITY_BOOST_WEIGHT * memoryCountWeight;

                for (const memoryId of linkedMemoryIds) {
                  if (memoryId) {
                    const memKey = String(memoryId);
                    entityMemoryIds.add(memKey);
                    entityBoosts[memKey] = Math.max(
                      entityBoosts[memKey] ?? 0,
                      boost,
                    );
                  }
                }
              }
            }
          }
        }
      } catch (e) {
        console.warn("Entity boost computation failed:", e);
      }
    }

    // Step 7: Build candidate set from semantic results
    const candidates = semanticResults
      .filter((mem) => showExpired || !payloadIsExpired(mem.payload))
      .map((mem) => ({
        id: String(mem.id),
        score: mem.score ?? 0,
        payload: mem.payload || {},
      }));

    // Step 8: Score and rank. Legacy mode intentionally keeps the former
    // semantic-only candidate pool and pre-fusion threshold semantics.
    let scoredResults: Array<{
      id: string;
      score: number;
      payload: Record<string, any>;
      scoreDetails?: Record<string, any>;
    }>;
    if (this.recallMode() === "legacy") {
      scoredResults = scoreAndRank(
        candidates,
        bm25Scores,
        entityBoosts,
        threshold ?? 0.1,
        topK,
        explain,
      );
    } else {
      if (keywordResults === null) {
        try {
          if (this.vectorStore.supportsCompleteScopedList !== true) {
            throw new Error("complete scoped listing is unavailable");
          }
          const [listed, total] = await this.completeScopeList(
            effectiveFilters,
            internalLimit,
          );
          keywordResults =
            total === listed.length
              ? lexicalFallbackResults(query, listed, {
                  topK: internalLimit,
                })
              : [];
        } catch {
          keywordResults = [];
        }
      }
      const entityResults = (
        await Promise.all(
          [...entityMemoryIds]
            .sort(
              (left, right) =>
                (entityBoosts[right] ?? 0) - (entityBoosts[left] ?? 0) ||
                left.localeCompare(right),
            )
            .map((memoryId) => this.vectorStore.get(memoryId)),
        )
      )
        .filter((row): row is VectorStoreResult => row !== null)
        .filter((row) =>
          this.payloadMatchesFilters(row.payload, effectiveFilters),
        );
      const visible = <T extends { payload: Record<string, any> }>(rows: T[]) =>
        rows.filter(
          (row) => showExpired || !payloadIsExpired(row.payload ?? {}),
        );
      const historical = isHistoricalQuery(query);
      scoredResults = reciprocalRankFusion(
        {
          semantic: visible(candidates),
          keyword: visible(keywordResults ?? []),
          entity: visible(entityResults),
        },
        {
          threshold: threshold ?? 0.1,
          topK,
          historical,
          historicalWindow: historicalQueryWindow(query),
          maxChars: config.maxChars,
          explain,
        },
      );
    }

    // Step 9: Format results
    const excludedKeys = new Set([
      "user_id",
      "agent_id",
      "run_id",
      "hash",
      "data",
      "createdAt",
      "updatedAt",
      "textLemmatized",
      "attributedTo",
    ]);

    const results = scoredResults
      .filter((scored) => scored.payload?.data)
      .map((scored) => {
        const payload = scored.payload || {};
        return {
          id: scored.id,
          memory: payload.data,
          hash: payload.hash,
          createdAt: payload.createdAt,
          updatedAt: payload.updatedAt,
          score: scored.score,
          metadata: Object.entries(payload)
            .filter(
              ([key]) =>
                !excludedKeys.has(key) && !key.startsWith(internalPrefix),
            )
            .reduce((acc, [key, value]) => ({ ...acc, [key]: value }), {}),
          ...(payload.user_id && { user_id: payload.user_id }),
          ...(payload.agent_id && { agent_id: payload.agent_id }),
          ...(payload.run_id && { run_id: payload.run_id }),
          ...(payload.attributedTo && { attributedTo: payload.attributedTo }),
          ...(scored.scoreDetails && { score_details: scored.scoreDetails }),
        };
      });

    // Step 10: Optionally re-rank with the configured reranker. Opt-in per
    // search via `rerank: true`; a no-op when no reranker is configured.
    const invokeReranker = Boolean(
      config.rerank && this.reranker && results.length > 0,
    );
    let finalResults = results;
    if (invokeReranker) {
      try {
        const ranked = await this.reranker!.rerank(
          query,
          results.map((r) => r.memory),
          topK,
        );
        finalResults = ranked.map((r) => ({
          ...results[r.index],
          rerankScore: r.rerankScore,
        }));
      } catch (e) {
        console.warn(`Reranking failed, using original results: ${e}`);
      }
    }

    const result = {
      results: finalResults,
    };
    const searchElapsedMs = Date.now() - searchStartMs;
    if (temporalUsageNotice) {
      await this._displayTemporalUsageNotice({
        triggerFunction: "search",
        triggerSource: temporalUsageNotice.triggerSource,
        triggerReason: temporalUsageNotice.triggerReason,
      });
    } else {
      const scaleThresholdNotice = detectScaleThresholdFromTopK(topK);
      if (scaleThresholdNotice) {
        await this._displayScaleThresholdNotice({
          triggerFunction: "search",
          ...scaleThresholdNotice,
        });
      } else {
        const performanceSlowQueryNotice = detectPerformanceSlowQuery(
          searchElapsedMs,
          topK,
          results.length,
        );
        if (performanceSlowQueryNotice) {
          await this._displayPerformanceSlowQueryNotice({
            triggerFunction: "search",
            triggerReason: "slow_query",
            ...performanceSlowQueryNotice,
          });
        } else {
          await this._displayFirstRunNotice("search");
        }
      }
    }
    return result;
  }

  private validateQualityFilters(
    filters: SearchFilters,
    limit: number,
  ): SearchFilters {
    validateSearchParams(undefined, limit);
    const normalized = { ...filters };
    for (const key of ["user_id", "agent_id", "run_id"] as const) {
      if (normalized[key] !== undefined) {
        normalized[key] = validateAndTrimEntityId(normalized[key], key);
      }
    }
    if (!normalized.user_id && !normalized.agent_id && !normalized.run_id) {
      throw new Error(
        "filters must contain at least one of: user_id, agent_id, run_id",
      );
    }
    return normalized;
  }

  async audit(
    filters: SearchFilters,
    options: QualityOperationOptions = {},
  ): Promise<ReturnType<typeof auditRows>> {
    await this._ensureInitialized();
    const limit = options.limit ?? 1000;
    const effectiveFilters = this.validateQualityFilters(filters, limit);
    const [rows, total] =
      this.vectorStore.supportsCompleteScopedList === true
        ? await this.completeScopeList(effectiveFilters, limit)
        : await this.vectorStore.list(effectiveFilters, limit);
    const report = auditRows(rows);
    if (
      this.vectorStore.supportsCompleteScopedList !== true ||
      typeof total !== "number" ||
      total !== rows.length
    ) {
      report.issues.providerInconsistencies = [
        ...new Set([
          ...report.issues.providerInconsistencies,
          "scope_listing_incomplete",
        ]),
      ];
      report.summary.providerInconsistencies =
        report.issues.providerInconsistencies.length;
    }
    return report;
  }

  async consolidate(
    filters: SearchFilters,
    options: ConsolidateMemoryOptions = {},
  ): Promise<{
    dryRun: boolean;
    strategy: "safe";
    scanned: number;
    actions: ReturnType<typeof safeConsolidationActions>;
    applied: number;
  }> {
    await this._ensureInitialized();
    const { dryRun = true, strategy = "safe", limit = 1000 } = options;
    if (strategy !== "safe") {
      throw new Error("Only the safe consolidation strategy is supported");
    }
    const effectiveFilters = this.validateQualityFilters(filters, limit);
    if (this.vectorStore.supportsCompleteScopedList !== true) {
      throw new MemoryPersistenceError(
        "Safe consolidation requires provider complete-list capability",
      );
    }
    const [rows, total] = await this.completeScopeList(effectiveFilters, limit);
    if (typeof total !== "number" || total !== rows.length) {
      throw new MemoryPersistenceError(
        "Safe consolidation requires a complete provider listing",
      );
    }
    const actions = safeConsolidationActions(rows);
    let applied = 0;
    if (!dryRun) {
      const changed: Array<{
        memoryId: string;
        oldPayload: Record<string, any>;
      }> = [];
      try {
        for (const action of actions) {
          const current = await this.vectorStore.get(action.memoryId);
          if (!current) {
            throw new MemoryPersistenceError(
              `Consolidation target ${action.memoryId} no longer exists`,
              { plannedIds: [action.memoryId] },
            );
          }
          const status =
            action.action === "archive" ? "archived" : "superseded";
          await this.replacePayload(action.memoryId, {
            ...current.payload,
            _mem0_status: status,
          });
          changed.push({
            memoryId: action.memoryId,
            oldPayload: { ...current.payload },
          });
          applied += 1;
        }
      } catch (cause) {
        const restored = await Promise.allSettled(
          changed
            .reverse()
            .map((item) => this.replacePayload(item.memoryId, item.oldPayload)),
        );
        if (restored.some((result) => result.status === "rejected")) {
          throw new MemoryPersistenceError(
            "Safe consolidation failed and one or more applied actions could not be restored",
            {
              plannedIds: actions.map((action) => action.memoryId),
              cause,
            },
          );
        }
        throw new MemoryPersistenceError(
          "Safe consolidation failed; applied actions were restored",
          {
            plannedIds: actions.map((action) => action.memoryId),
            cause,
          },
        );
      }
    }
    return { dryRun, strategy, scanned: rows.length, actions, applied };
  }

  async feedback(
    memoryId: string,
    options: FeedbackMemoryOptions,
  ): Promise<{ message: string }> {
    await this._ensureInitialized();
    if (
      !["helpful", "irrelevant", "stale", "incorrect"].includes(options.signal)
    ) {
      throw new Error(
        "signal must be one of: helpful, irrelevant, stale, incorrect",
      );
    }
    const current = await this.vectorStore.get(memoryId);
    if (!current) throw new Error(`Memory with ID ${memoryId} not found`);
    const next = { ...current.payload };
    const field = `_mem0_feedback_${options.signal}`;
    next[field] = Number(next[field] ?? 0) + 1;
    if (options.signal === "helpful") {
      next._mem0_salience = Math.min(
        1,
        Number(next._mem0_salience ?? 0.5) + 0.1,
      );
    } else if (options.signal === "stale") {
      next._mem0_status = "archived";
    } else if (options.signal === "incorrect") {
      next._mem0_status = "invalid";
    }
    if (options.replacementId) {
      const replacement = await this.vectorStore.get(options.replacementId);
      if (!replacement) {
        throw new Error(
          `Replacement memory with ID ${options.replacementId} not found`,
        );
      }
      for (const key of ["user_id", "agent_id", "run_id"] as const) {
        if (current.payload[key] !== replacement.payload[key]) {
          throw new Error(
            "Replacement memory must use the same identity scope",
          );
        }
      }
      next._mem0_replacement_id = options.replacementId;
    }
    await this.replacePayload(memoryId, next);
    return { message: "Memory feedback recorded successfully!" };
  }

  async update(
    memoryId: string,
    config: string | UpdateMemoryOptions,
  ): Promise<{ message: string }> {
    await this._ensureInitialized();
    await this._captureEvent("update", { memory_id: memoryId });

    const options: UpdateMemoryOptions =
      typeof config === "string" ? { text: config } : config;

    const { data, metadata, expirationDate } = options;
    let text = options.text;

    if (data != null) {
      logger.warn(
        "The `data` option of update() is deprecated and will be removed in " +
          "the next major release. Use `text` instead.",
      );
      if (text == null) {
        text = data;
      }
    }

    if (text == null && metadata == null && expirationDate === undefined) {
      throw new Error(
        "At least one of text, metadata, or expirationDate must be provided.",
      );
    }

    const updateMetadata: Record<string, any> = stripInternalMetadata(metadata);
    if (expirationDate !== undefined) {
      updateMetadata.expiration_date =
        expirationDate === null
          ? null
          : normalizeExpirationDate(expirationDate);
    }

    const existingEmbeddings: Record<string, number[]> = {};
    if (text != null) {
      existingEmbeddings[text] = await this.embedder.embed(text, "update");
    }

    await this.updateMemory(memoryId, text, existingEmbeddings, updateMetadata);
    const result = { message: "Memory updated successfully!" };
    await this._displayFirstRunNotice("update");
    return result;
  }

  async delete(memoryId: string): Promise<{ message: string }> {
    await this._ensureInitialized();
    await this._captureEvent("delete", { memory_id: memoryId });
    await this.deleteMemory(memoryId);
    const result = { message: "Memory deleted successfully!" };
    const deleteCount = getDecayUsageDeleteCountAfterSuccess();
    if (isDecayUsageDeleteEligible(deleteCount)) {
      await this._displayDecayUsageNotice({
        triggerFunction: "delete",
        triggerSource: "delete_count",
        triggerReason: "repeated_deletes",
        deleteCount,
      });
    } else {
      await this._displayFirstRunNotice("delete");
    }
    return result;
  }

  async deleteAll(
    config: DeleteAllMemoryOptions,
  ): Promise<{ message: string }> {
    await this._ensureInitialized();
    await this._captureEvent("delete_all", {
      has_user_id: !!config.userId,
      has_agent_id: !!config.agentId,
      has_run_id: !!config.runId,
    });
    const userId = validateAndTrimEntityId(config.userId, "userId");
    const agentId = validateAndTrimEntityId(config.agentId, "agentId");
    const runId = validateAndTrimEntityId(config.runId, "runId");

    // Convert camelCase entity params to snake_case for filters (matches storage and search/getAll)
    const filters: SearchFilters = {};
    if (userId) filters.user_id = userId;
    if (agentId) filters.agent_id = agentId;
    if (runId) filters.run_id = runId;

    if (!Object.keys(filters).length) {
      throw new Error(
        "At least one filter is required to delete all memories. If you want to delete all memories, use the `reset()` method.",
      );
    }

    let deletedCount = 0;
    const seenBatches = new Set<string>();

    while (true) {
      const [batch] = await this.vectorStore.list(
        filters,
        DELETE_ALL_BATCH_SIZE,
      );
      if (!batch.length) {
        break;
      }
      const batchKey = batch
        .map((memory) => String(memory.id))
        .sort()
        .join(",");
      if (seenBatches.has(batchKey)) {
        logger.warn("Stopping deleteAll after a repeated memory batch");
        break;
      }
      seenBatches.add(batchKey);
      for (const memory of batch) {
        await this.deleteMemory(memory.id);
      }
      deletedCount += batch.length;
    }

    const result = { message: "Memories deleted successfully!" };
    if (deletedCount > 0) {
      await this._displayDecayUsageNotice({
        triggerFunction: "delete_all",
        triggerSource: "delete_all",
        triggerReason: "bulk_delete",
        deletedCount,
      });
    } else {
      await this._displayFirstRunNotice("delete_all");
    }
    return result;
  }

  async history(memoryId: string): Promise<any[]> {
    await this._ensureInitialized();
    const result = await this.db.getHistory(memoryId);
    await this._displayFirstRunNotice("history");
    return result;
  }

  async reset(): Promise<void> {
    await this._ensureInitialized();
    await this._captureEvent("reset");
    await this.db.reset();

    // Check provider before attempting deleteCol
    if (this.config.vectorStore.provider !== "langchain") {
      try {
        await this.vectorStore.deleteCol();
      } catch (e) {
        console.error(
          `Failed to delete collection for provider '${this.config.vectorStore.provider}':`,
          e,
        );
        // Decide if you want to re-throw or just log
      }
    } else {
      console.warn(
        "Memory.reset(): Skipping vector store collection deletion as 'langchain' provider is used. Underlying Langchain vector store data is not cleared by this operation.",
      );
    }

    if (this._entityStore) {
      try {
        await this._entityStore.deleteCol();
      } catch {}
      this._entityStore = undefined;
    }

    // Re-initialize factories/clients based on the original config.
    // Dimension is already set in this.config from the initial probe,
    // so _autoInitialize will skip the probe and just re-create the store.
    this.embedder = EmbedderFactory.create(
      this.config.embedder.provider,
      this.config.embedder.config,
    );
    this.llm = LLMFactory.create(
      this.config.llm.provider,
      this.config.llm.config,
    );

    // Re-create vector store via _autoInitialize (which handles dimension + creation)
    this._initError = undefined;
    this._initPromise = this._autoInitialize().catch((error) => {
      this._initError =
        error instanceof Error ? error : new Error(String(error));
      console.error(this._initError);
    });
    await this._initPromise;
    await this._displayFirstRunNotice("reset");
  }

  async getAll(config: GetAllMemoryOptions): Promise<SearchResult> {
    // Reject top-level entity params - must use filters instead
    rejectTopLevelEntityParams(config as Record<string, any>, "getAll");

    // Validate topK if provided (before applying defaults)
    validateSearchParams(undefined, config.topK);

    await this._ensureInitialized();

    const { topK = 20, showExpired = false } = config;

    // Validate and trim entity IDs in filters. Drop keys that resolve to
    // undefined so downstream vector stores don't receive
    // `agent_id: undefined` / `run_id: undefined` and fail.
    const filters: Record<string, any> = Object.fromEntries(
      Object.entries({
        ...(config.filters || {}),
        user_id: validateAndTrimEntityId(config.filters?.user_id, "user_id"),
        agent_id: validateAndTrimEntityId(config.filters?.agent_id, "agent_id"),
        run_id: validateAndTrimEntityId(config.filters?.run_id, "run_id"),
      }).filter(([, v]) => v !== undefined),
    );

    await this._captureEvent("get_all", {
      topK,
      has_user_id: !!filters.user_id,
      has_agent_id: !!filters.agent_id,
      has_run_id: !!filters.run_id,
    });

    // Validate filters contains at least one entity ID (snake_case)
    if (!filters.user_id && !filters.agent_id && !filters.run_id) {
      throw new Error(
        "filters must contain at least one of: user_id, agent_id, run_id. " +
          "Example: filters: { user_id: 'u1' }",
      );
    }

    // Over-fetch so expired memories dropped below still leave topK survivors.
    const fetchLimit = showExpired ? topK : Math.max(topK * 4, 60);
    const [memories] = await this.vectorStore.list(filters, fetchLimit);

    const visibleMemories = showExpired
      ? memories
      : memories.filter((mem) => !payloadIsExpired(mem.payload));

    const excludedKeys = new Set([
      "user_id",
      "agent_id",
      "run_id",
      "hash",
      "data",
      "createdAt",
      "updatedAt",
      "textLemmatized",
      "attributedTo",
    ]);
    const results = visibleMemories.slice(0, topK).map((mem) => ({
      id: mem.id,
      memory: mem.payload.data,
      hash: mem.payload.hash,
      createdAt: mem.payload.createdAt,
      updatedAt: mem.payload.updatedAt,
      metadata: Object.entries(mem.payload)
        .filter(
          ([key]) => !excludedKeys.has(key) && !key.startsWith(internalPrefix),
        )
        .reduce((acc, [key, value]) => ({ ...acc, [key]: value }), {}),
      ...(mem.payload.user_id && { user_id: mem.payload.user_id }),
      ...(mem.payload.agent_id && { agent_id: mem.payload.agent_id }),
      ...(mem.payload.run_id && { run_id: mem.payload.run_id }),
      ...(mem.payload.attributedTo && {
        attributedTo: mem.payload.attributedTo,
      }),
    }));

    const result = { results };
    const scaleThresholdNotice = detectScaleThresholdFromTopK(topK);
    if (scaleThresholdNotice) {
      await this._displayScaleThresholdNotice({
        triggerFunction: "get_all",
        ...scaleThresholdNotice,
      });
    } else {
      await this._displayFirstRunNotice("get_all");
    }
    return result;
  }

  private async createMemory(
    data: string,
    existingEmbeddings: Record<string, number[]>,
    metadata: Record<string, any>,
  ): Promise<string> {
    const memoryId = uuidv4();
    const embedding =
      existingEmbeddings[data] || (await this.embedder.embed(data, "add"));

    const memoryMetadata = {
      ...metadata,
      data,
      hash: createHash("md5").update(data).digest("hex"),
      textLemmatized: lemmatizeForBm25(data),
      createdAt: new Date().toISOString(),
    };

    await this.vectorStore.insert([embedding], [memoryId], [memoryMetadata]);
    await this.db.addHistory(
      memoryId,
      null,
      data,
      "ADD",
      memoryMetadata.createdAt,
    );

    return memoryId;
  }

  private async updateMemory(
    memoryId: string,
    data: string | undefined,
    existingEmbeddings: Record<string, number[]>,
    metadata: Record<string, any> = {},
  ): Promise<string> {
    const existingMemory = await this.vectorStore.get(memoryId);
    if (!existingMemory) {
      throw new Error(`Memory with ID ${memoryId} not found`);
    }

    const prevValue = existingMemory.payload.data;
    // Metadata-only update: fall back to the stored text so we can re-index it.
    const newData = data ?? prevValue;
    if (typeof newData !== "string") {
      throw new Error(
        `Memory with ID ${memoryId} does not have text content to update`,
      );
    }
    const textChanged = newData !== prevValue;
    if (
      this.recallMode() === "stateAware" &&
      textChanged &&
      existingMemory.payload._mem0_schema_version === schemaVersion &&
      stateLikeKinds.has(existingMemory.payload._mem0_kind)
    ) {
      throw new MemoryPersistenceError(
        "State-aware memories cannot be edited in place; add the correction as new evidence",
        { plannedIds: [memoryId] },
      );
    }

    const embedding =
      existingEmbeddings[newData] ||
      (await this.embedder.embed(newData, "update"));

    // Caller metadata must not overwrite or inject an identity scope (#6342 / #6367).
    const sanitizedMetadata = Object.fromEntries(
      Object.entries(metadata).filter(([k]) => !IDENTITY_KEYS.includes(k)),
    );

    const newMetadata = {
      ...existingMemory.payload,
      ...sanitizedMetadata,
      data: newData,
      hash: createHash("md5").update(newData).digest("hex"),
      textLemmatized: lemmatizeForBm25(newData),
      createdAt: existingMemory.payload.createdAt,
      updatedAt: new Date().toISOString(),
    };

    await this.vectorStore.update(memoryId, embedding, newMetadata);
    await this.db.addHistory(
      memoryId,
      prevValue,
      newData,
      "UPDATE",
      newMetadata.createdAt,
      newMetadata.updatedAt,
    );

    // Entity-store cleanup only when the text changed: strip this memory's id
    // from old-text entities, then re-extract from the new text and link back.
    if (textChanged) {
      try {
        const sessionFilters = this._sessionFiltersFromPayload(newMetadata);
        await this._removeMemoryFromEntityStore(memoryId, sessionFilters);
        await this._linkEntitiesForMemory(memoryId, newData, sessionFilters);
      } catch (e) {
        console.warn(`Entity store cleanup/link failed during update: ${e}`);
      }
    }

    return memoryId;
  }

  private async deleteMemory(memoryId: string): Promise<string> {
    const existingMemory = await this.vectorStore.get(memoryId);
    if (!existingMemory) {
      throw new Error(`Memory with ID ${memoryId} not found`);
    }

    const prevValue = existingMemory.payload.data;
    const sessionFilters = this._sessionFiltersFromPayload(
      existingMemory.payload || {},
    );
    await this.vectorStore.delete(memoryId);
    await this.db.addHistory(
      memoryId,
      prevValue,
      null,
      "DELETE",
      undefined,
      undefined,
      1,
    );

    // Entity-store cleanup: strip this memory's id from any entity records
    // that linked to it. Non-fatal — log and continue on error.
    try {
      await this._removeMemoryFromEntityStore(memoryId, sessionFilters);
    } catch (e) {
      console.warn(`Entity store cleanup failed during delete: ${e}`);
    }

    return memoryId;
  }

  /**
   * Check if filters contain advanced operators that need special processing.
   */
  private _hasAdvancedOperators(filters: Record<string, any>): boolean {
    if (!filters || typeof filters !== "object") {
      return false;
    }

    for (const [key, value] of Object.entries(filters)) {
      // Check for platform-style logical operators
      if (key === "AND" || key === "OR" || key === "NOT") {
        return true;
      }
      // Check for comparison operators
      if (
        typeof value === "object" &&
        value !== null &&
        !Array.isArray(value)
      ) {
        for (const op of Object.keys(value)) {
          if (
            [
              "eq",
              "ne",
              "gt",
              "gte",
              "lt",
              "lte",
              "in",
              "nin",
              "contains",
              "icontains",
            ].includes(op)
          ) {
            return true;
          }
        }
      }
      // Check for wildcard values
      if (value === "*") {
        return true;
      }
    }
    return false;
  }

  /**
   * Process enhanced metadata filters and convert them to vector store compatible format.
   * Converts AND/OR/NOT to $or/$not format that vector stores can interpret.
   */
  private _processMetadataFilters(
    metadataFilters: Record<string, any>,
  ): Record<string, any> {
    const processedFilters: Record<string, any> = {};

    const processCondition = (
      key: string,
      condition: any,
    ): Record<string, any> => {
      if (typeof condition !== "object" || condition === null) {
        // Simple equality: {"key": "value"} or wildcard
        if (condition === "*") {
          return { [key]: "*" };
        }
        return { [key]: condition };
      }

      if (Array.isArray(condition)) {
        // Array shorthand for "in" operator
        return { [key]: { in: condition } };
      }

      const result: Record<string, any> = {};
      const operatorMap: Record<string, string> = {
        eq: "eq",
        ne: "ne",
        gt: "gt",
        gte: "gte",
        lt: "lt",
        lte: "lte",
        in: "in",
        nin: "nin",
        contains: "contains",
        icontains: "icontains",
      };

      for (const [operator, value] of Object.entries(condition)) {
        if (operator in operatorMap) {
          if (!result[key]) {
            result[key] = {};
          }
          result[key][operatorMap[operator]] = value;
        } else {
          throw new Error(`Unsupported metadata filter operator: ${operator}`);
        }
      }
      return result;
    };

    for (const [key, value] of Object.entries(metadataFilters)) {
      if (key === "AND") {
        // Logical AND: combine multiple conditions
        if (!Array.isArray(value)) {
          throw new Error("AND operator requires a list of conditions");
        }
        for (const condition of value) {
          for (const [subKey, subValue] of Object.entries(condition)) {
            Object.assign(processedFilters, processCondition(subKey, subValue));
          }
        }
      } else if (key === "OR") {
        // Logical OR: Pass through to vector store for implementation-specific handling
        if (!Array.isArray(value) || value.length === 0) {
          throw new Error(
            "OR operator requires a non-empty list of conditions",
          );
        }
        processedFilters["$or"] = [];
        for (const condition of value) {
          const orCondition: Record<string, any> = {};
          for (const [subKey, subValue] of Object.entries(
            condition as Record<string, any>,
          )) {
            Object.assign(orCondition, processCondition(subKey, subValue));
          }
          processedFilters["$or"].push(orCondition);
        }
      } else if (key === "NOT") {
        // Logical NOT: Pass through to vector store for implementation-specific handling
        if (!Array.isArray(value) || value.length === 0) {
          throw new Error(
            "NOT operator requires a non-empty list of conditions",
          );
        }
        processedFilters["$not"] = [];
        for (const condition of value) {
          const notCondition: Record<string, any> = {};
          for (const [subKey, subValue] of Object.entries(
            condition as Record<string, any>,
          )) {
            Object.assign(notCondition, processCondition(subKey, subValue));
          }
          processedFilters["$not"].push(notCondition);
        }
      } else {
        Object.assign(processedFilters, processCondition(key, value));
      }
    }

    return processedFilters;
  }
}

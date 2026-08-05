import { SearchFilters, VectorStoreResult } from "../types";

export interface VectorStore {
  /**
   * True only when list(filters, topK) returns a true scoped total, allowing
   * callers to prove that a page below their safety bound is complete.
   */
  readonly supportsCompleteScopedList?: boolean;
  insert(
    vectors: number[][],
    ids: string[],
    payloads: Record<string, any>[],
  ): Promise<void>;
  search(
    query: number[],
    topK?: number,
    filters?: SearchFilters,
  ): Promise<VectorStoreResult[]>;
  keywordSearch?(
    query: string,
    topK?: number,
    filters?: SearchFilters,
  ): Promise<VectorStoreResult[] | null>;
  get(vectorId: string): Promise<VectorStoreResult | null>;
  update(
    vectorId: string,
    vector: number[],
    payload: Record<string, any>,
  ): Promise<void>;
  delete(vectorId: string): Promise<void>;
  deleteCol(): Promise<void>;
  list(
    filters?: SearchFilters,
    topK?: number,
  ): Promise<[VectorStoreResult[], number]>;
  /** Optional exact-count variant used by state-aware lifecycle operations. */
  listComplete?(
    filters?: SearchFilters,
    topK?: number,
  ): Promise<[VectorStoreResult[], number]>;
  getUserId(): Promise<string>;
  setUserId(userId: string): Promise<void>;
  initialize(): Promise<void>;
}

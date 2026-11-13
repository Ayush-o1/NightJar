/* ============================================
   NightJar — demo dataset
   ============================================
   Used only when the dashboard can't reach a backend (see
   checkAPIConnection in ui.js), and the connection badge says "Demo data"
   whenever it is. It exists so the UI is still browsable without a running
   server — not to dress up a disconnected dashboard as a live one.

   These records go through the same aggregation functions in app.js
   (aggregateByDay, aggregateByReason, tierCounts, similarityScoresFromHits)
   as real API records, so there's one code path for turning a request list
   into charts and tables rather than two that can drift apart. */

function _hoursAgo(h) {
  const dt = new Date();
  dt.setHours(dt.getHours() - h, Math.floor(Math.random() * 60), Math.floor(Math.random() * 60));
  return dt.toISOString();
}

// ─── DEMO REQUESTS ───────────────────────────────────────────
// Same shape the dashboard normalizes real API records into (see
// normalizeApiRecord in app.js): id, timestamp, model, tokens_in/out,
// latency_ms, cost, cache_status, similarity_score, tier, routing_reason.
const DEMO_REQUESTS = [
  { id: 'req_a1b2c3d4', timestamp: _hoursAgo(0.2), model: 'gpt-4o', tokens_in: 1842, tokens_out: 512, latency_ms: 1243, cost: 0.0387, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'complex_keyword:analyze' },
  { id: 'req_e5f6g7h8', timestamp: _hoursAgo(0.5), model: 'gpt-4o-mini', tokens_in: 423, tokens_out: 189, latency_ms: 312, cost: 0.0012, cache_status: 'HIT', similarity_score: 0.97, tier: 'simple', routing_reason: 'token_count:180<=200' },
  { id: 'req_i9j0k1l2', timestamp: _hoursAgo(0.8), model: 'claude-3.5-sonnet', tokens_in: 2105, tokens_out: 743, latency_ms: 2156, cost: 0.0521, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'token_count:2105>=500' },
  { id: 'req_m3n4o5p6', timestamp: _hoursAgo(1.1), model: 'gpt-4o', tokens_in: 956, tokens_out: 287, latency_ms: 834, cost: 0.0198, cache_status: 'HIT', similarity_score: 0.94, tier: 'complex', routing_reason: 'complex_keyword:refactor' },
  { id: 'req_q7r8s9t0', timestamp: _hoursAgo(1.5), model: 'gemini-1.5-pro', tokens_in: 1567, tokens_out: 621, latency_ms: 1879, cost: 0.0312, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'complex_keyword:debug' },
  { id: 'req_u1v2w3x4', timestamp: _hoursAgo(2.0), model: 'claude-3-haiku', tokens_in: 324, tokens_out: 156, latency_ms: 198, cost: 0.0004, cache_status: 'HIT', similarity_score: 0.99, tier: 'simple', routing_reason: 'simple_keyword:hi' },
  { id: 'req_y5z6a7b8', timestamp: _hoursAgo(2.3), model: 'gpt-4o-mini', tokens_in: 712, tokens_out: 234, latency_ms: 445, cost: 0.0018, cache_status: 'MISS', similarity_score: null, tier: 'simple', routing_reason: 'token_count:190<=200' },
  { id: 'req_c9d0e1f2', timestamp: _hoursAgo(2.8), model: 'gpt-4o', tokens_in: 2341, tokens_out: 892, latency_ms: 2876, cost: 0.0612, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'complex_keyword:architect' },
  { id: 'req_g3h4i5j6', timestamp: _hoursAgo(3.2), model: 'gemini-1.5-flash', tokens_in: 543, tokens_out: 198, latency_ms: 267, cost: 0.0008, cache_status: 'HIT', similarity_score: 0.92, tier: 'simple', routing_reason: 'token_count:150<=200' },
  { id: 'req_k7l8m9n0', timestamp: _hoursAgo(3.6), model: 'claude-3.5-sonnet', tokens_in: 1876, tokens_out: 654, latency_ms: 1923, cost: 0.0478, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'token_count:1876>=500' },
  { id: 'req_o1p2q3r4', timestamp: _hoursAgo(4.0), model: 'gpt-4o', tokens_in: 1123, tokens_out: 412, latency_ms: 1067, cost: 0.0267, cache_status: 'HIT', similarity_score: 0.95, tier: 'complex', routing_reason: 'complex_keyword:optimize' },
  { id: 'req_s5t6u7v8', timestamp: _hoursAgo(4.5), model: 'gpt-4o-mini', tokens_in: 287, tokens_out: 98, latency_ms: 187, cost: 0.0006, cache_status: 'HIT', similarity_score: 0.98, tier: 'simple', routing_reason: 'token_count:95<=200' },
  { id: 'req_w9x0y1z2', timestamp: _hoursAgo(5.0), model: 'claude-3-haiku', tokens_in: 456, tokens_out: 167, latency_ms: 213, cost: 0.0005, cache_status: 'MISS', similarity_score: null, tier: 'simple', routing_reason: 'simple_keyword:thanks' },
  { id: 'req_a3b4c5d6', timestamp: _hoursAgo(5.5), model: 'gemini-1.5-pro', tokens_in: 1987, tokens_out: 756, latency_ms: 2234, cost: 0.0389, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'token_count:1987>=500' },
  { id: 'req_e7f8g9h0', timestamp: _hoursAgo(6.2), model: 'gpt-4o', tokens_in: 834, tokens_out: 312, latency_ms: 756, cost: 0.0178, cache_status: 'HIT', similarity_score: 0.91, tier: 'complex', routing_reason: 'complex_keyword:security' },
  { id: 'req_m5n6o7p8', timestamp: _hoursAgo(8.0), model: 'claude-3.5-sonnet', tokens_in: 1654, tokens_out: 543, latency_ms: 1678, cost: 0.0398, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'token_count:1654>=500' },
  { id: 'req_q9r0s1t2', timestamp: _hoursAgo(9.5), model: 'gpt-4o', tokens_in: 765, tokens_out: 234, latency_ms: 623, cost: 0.0156, cache_status: 'HIT', similarity_score: 0.96, tier: 'complex', routing_reason: 'complex_keyword:performance' },
  { id: 'req_u3v4w5x6', timestamp: _hoursAgo(10.0), model: 'gemini-1.5-flash', tokens_in: 432, tokens_out: 176, latency_ms: 234, cost: 0.0006, cache_status: 'HIT', similarity_score: 0.93, tier: 'simple', routing_reason: 'token_count:120<=200' },
  { id: 'req_y7z8a9b0', timestamp: _hoursAgo(11.0), model: 'claude-3-haiku', tokens_in: 567, tokens_out: 213, latency_ms: 289, cost: 0.0006, cache_status: 'MISS', similarity_score: null, tier: 'simple', routing_reason: 'token_count:180<=200' },
  { id: 'req_c1d2e3f4', timestamp: _hoursAgo(13.0), model: 'gpt-4o', tokens_in: 2456, tokens_out: 987, latency_ms: 3012, cost: 0.0678, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'token_count:2456>=500' },
  { id: 'req_g5h6i7j8', timestamp: _hoursAgo(15.0), model: 'gpt-4o-mini', tokens_in: 345, tokens_out: 123, latency_ms: 213, cost: 0.0007, cache_status: 'HIT', similarity_score: 0.97, tier: 'simple', routing_reason: 'token_count:100<=200' },
  { id: 'req_k9l0m1n2', timestamp: _hoursAgo(18.0), model: 'claude-3.5-sonnet', tokens_in: 1432, tokens_out: 521, latency_ms: 1534, cost: 0.0356, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'token_count:1432>=500' },
  { id: 'req_o3p4q5r6', timestamp: _hoursAgo(20.0), model: 'gpt-4o', tokens_in: 987, tokens_out: 345, latency_ms: 892, cost: 0.0212, cache_status: 'HIT', similarity_score: 0.94, tier: 'complex', routing_reason: 'complex_keyword:implement' },
  { id: 'req_s7t8u9v0', timestamp: _hoursAgo(22.0), model: 'gemini-1.5-pro', tokens_in: 1789, tokens_out: 678, latency_ms: 2089, cost: 0.0356, cache_status: 'MISS', similarity_score: null, tier: 'complex', routing_reason: 'token_count:1789>=500' },
];

// ─── DEMO SUMMARY ────────────────────────────────────────────
const DEMO_SUMMARY = {
  total_requests: 23,
  cache_hit_rate: 43.5,
  avg_latency_ms: 1051,
  total_cost: 0.44,
};

// ─── DEMO CACHE STATS ────────────────────────────────────────
// Mirrors exactly what GET /v1/cache/stats returns for real — nothing more.
const DEMO_CACHE_STATS = {
  total_vectors: 23,
  redis_keys: 21,
  similarity_threshold: 0.92,
};

// ─── FIELD ENRICHMENT ────────────────────────────────────────
// Gives every demo record the same field names normalizeApiRecord() produces
// for real API records, so table and chart code never branches on the source.
DEMO_REQUESTS.forEach(r => {
  r.request_id = r.id;
  r.model_used = r.model;
  r.cache_hit = r.cache_status === 'HIT';
  r.prompt_tokens = r.tokens_in;
  r.completion_tokens = r.tokens_out;
  r.estimated_cost_usd = r.cost;
});

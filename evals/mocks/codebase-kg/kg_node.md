{
  "id": "feed_ranker",
  "kind": "Domain (pure)",
  "description": "Ranks the main feed by freshness (6h half-life), per-source weight and a breaking-news boost. Stateless; sorts descending by score.",
  "anchors": ["domain/FeedRanker.kt#FeedRanker"],
  "edges": ["article"],
  "section": "DOMAIN",
  "found": true,
  "inbound_edges": ["feed_view_model"]
}

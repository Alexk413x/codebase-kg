{
  "path": "domain/FeedRanker.kt",
  "count": 1,
  "nodes": [
    {
      "id": "feed_ranker",
      "kind": "Domain (pure)",
      "description": "Ranks the main feed by freshness (6h half-life), per-source weight and a breaking-news boost. Stateless; sorts descending by score.",
      "matched_anchors": ["domain/FeedRanker.kt#FeedRanker"],
      "edges": ["article"],
      "inbound_edges": ["feed_view_model"]
    }
  ]
}

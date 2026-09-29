---
expect:
  query: string
---

{
  "query": "feed ranking",
  "count": 3,
  "results": [
    {
      "id": "feed_ranker",
      "kind": "Domain (pure)",
      "section": "DOMAIN",
      "description": "Ranks the main feed by freshness (6h half-life), per-source weight and a breaking-news boost. Stateless; sorts descending by score.",
      "anchors": ["domain/FeedRanker.kt#FeedRanker"],
      "parity": null,
      "score": 9.4
    },
    {
      "id": "feed_view_model",
      "kind": "ViewModel",
      "section": "UI",
      "description": "Loads the latest articles and passes them through FeedRanker before the feed screen renders them.",
      "anchors": ["ui/FeedViewModel.kt#FeedViewModel"],
      "parity": null,
      "score": 3.1
    },
    {
      "id": "feed_screen",
      "kind": "Screen",
      "section": "UI",
      "description": "The main feed screen; renders the ranked article titles from FeedViewModel.",
      "anchors": ["ui/FeedScreen.kt#FeedScreen"],
      "parity": null,
      "score": 2.6
    }
  ]
}

{
  "found": true,
  "center": {
    "id": "feed_ranker",
    "kind": "Domain (pure)",
    "description": "Ranks the main feed by freshness (6h half-life), per-source weight and a breaking-news boost. Stateless; sorts descending by score.",
    "anchors": ["domain/FeedRanker.kt#FeedRanker"],
    "edges": ["article"],
    "section": "DOMAIN"
  },
  "depth": 2,
  "inbound_edges": ["feed_view_model"],
  "count": 3,
  "neighbors": [
    {
      "id": "article",
      "kind": "Data model",
      "description": "Article value type and the repository that loads the latest articles from the API.",
      "anchors": ["data/ArticleRepository.kt#Article", "data/ArticleRepository.kt#ArticleRepository"],
      "hops": 1
    },
    {
      "id": "feed_view_model",
      "kind": "ViewModel",
      "description": "Loads the latest articles and passes them through FeedRanker before the feed screen renders them.",
      "anchors": ["ui/FeedViewModel.kt#FeedViewModel"],
      "hops": 1
    },
    {
      "id": "feed_screen",
      "kind": "Screen",
      "description": "The main feed screen; renders the ranked article titles from FeedViewModel.",
      "anchors": ["ui/FeedScreen.kt#FeedScreen"],
      "hops": 2
    }
  ],
  "counterpart": null
}

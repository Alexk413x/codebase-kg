package app.ui

import app.data.Article
import app.data.ArticleRepository
import app.domain.FeedRanker

class FeedViewModel(
    private val repository: ArticleRepository,
    private val sourceWeights: Map<String, Double>,
    private val clock: () -> Long,
) {
    suspend fun loadFeed(): List<Article> =
        FeedRanker.rank(repository.latest(), sourceWeights, clock())
}

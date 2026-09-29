package app.domain

import app.data.Article

object FeedRanker {
    private const val FRESHNESS_HALF_LIFE_HOURS = 6.0
    private const val BREAKING_BOOST = 2.5

    fun rank(articles: List<Article>, sourceWeights: Map<String, Double>, nowMillis: Long): List<Article> =
        articles.sortedByDescending { score(it, sourceWeights, nowMillis) }

    fun score(article: Article, sourceWeights: Map<String, Double>, nowMillis: Long): Double {
        val ageHours = (nowMillis - article.publishedAtMillis) / 3_600_000.0
        val freshness = Math.pow(0.5, ageHours / FRESHNESS_HALF_LIFE_HOURS)
        val weight = sourceWeights[article.source] ?: 1.0
        val boost = if (article.isBreaking) BREAKING_BOOST else 1.0
        return freshness * weight * boost
    }
}

package app.data

data class Article(
    val id: String,
    val title: String,
    val source: String,
    val publishedAtMillis: Long,
    val isBreaking: Boolean = false,
)

class ArticleRepository(private val api: ArticleApi) {
    suspend fun latest(): List<Article> = api.fetchLatest()
}

interface ArticleApi {
    suspend fun fetchLatest(): List<Article>
}

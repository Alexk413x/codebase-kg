package app.room

data class SavedArticleEntity(
    val articleId: String,
    val title: String,
    val savedAtMillis: Long,
)

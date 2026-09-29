package app.ui

import app.data.Article

class FeedScreen(private val viewModel: FeedViewModel) {
    suspend fun render(): List<String> = viewModel.loadFeed().map(Article::title)
}

package app.ui

import app.room.SavedArticleEntity

class BookmarksScreen(private val saved: List<SavedArticleEntity>) {
    fun render(): List<String> = saved.map { it.title }
}

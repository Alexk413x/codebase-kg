---
type: llm
focus: last_message
---

The repo has one class that depends on FeedRanker directly, FeedViewModel, and one that depends on FeedViewModel, FeedScreen. FeedRanker itself uses the Article type from ArticleRepository.kt.

PASS if the answer names FeedViewModel as a dependent of FeedRanker. Naming FeedScreen as an indirect dependent is expected but not required.
FAIL if the answer omits FeedViewModel, or presents classes that do not exist in this repo (for example a cache, a database layer or a test suite) as dependents.

"""JSONPlaceholder API client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import requests

API_URL = "https://jsonplaceholder.typicode.com/posts"


@dataclass
class Post:
    id: int
    title: str
    body: str


def fetch_posts(limit: int = 10, timeout: int = 10) -> List[Post]:
    """Fetch posts from JSONPlaceholder. Raises on network/API failure."""
    response = requests.get(API_URL, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    posts = [Post(id=item["id"], title=item["title"], body=item["body"]) for item in data]
    return posts[:limit]


def format_post(post: Post) -> str:
    """Format a post for Notepad output."""
    return f"Title: {post.title}\n\n{post.body}"

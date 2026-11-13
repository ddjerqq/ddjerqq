"""Generate the GitHub profile SVGs.

The script deliberately remains a single file, but the work is split into
small classes so that API access, caching, and rendering are independently
understandable and testable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from dateutil import relativedelta
from lxml import etree


GRAPHQL_URL = "https://api.github.com/graphql"
REPOSITORY_QUERY = """
query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
    user(login: $login) {
        repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history { totalCount }
                                }
                            }
                        }
                    }
                }
            }
            pageInfo { endCursor hasNextPage }
        }
    }
}
"""
COMMIT_QUERY = """
query ($repo_name: String!, $owner: String!, $cursor: String) {
    repository(name: $repo_name, owner: $owner) {
        defaultBranchRef {
            target {
                ... on Commit {
                    history(first: 100, after: $cursor) {
                        edges {
                            node {
                                ... on Commit {
                                    additions
                                    deletions
                                    parents { totalCount }
                                }
                                author { user { id } }
                            }
                        }
                        pageInfo { endCursor hasNextPage }
                    }
                }
            }
        }
    }
}
"""
STARS_QUERY = """
query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
    user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
            totalCount
            edges { node { ... on Repository { stargazers { totalCount } } } }
            pageInfo { endCursor hasNextPage }
        }
    }
}
"""
REPOSITORIES_QUERY = """
query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
    user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
            totalCount
            pageInfo { endCursor hasNextPage }
        }
    }
}
"""


@dataclass
class Settings:
    username: str = field(default_factory=lambda: os.getenv("GITHUB_USERNAME", "ddjerqq"))
    token: str = field(default_factory=lambda: os.getenv("ACCESS_TOKEN"))
    cache_dir: Path = Path("cache")
    comment_size: int = 7
    cache_version: str = "cache-format: 3"

    @property
    def cache_file(self) -> Path:
        digest = hashlib.sha256(self.username.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.txt"


class GitHubClient:
    """Small GraphQL client with safe cursor pagination."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.query_counts: dict[str, int] = {}
        self.session = requests.Session()
        self.session.headers.update({"authorization": f"token {settings.token}"})

    def _request(
        self,
        operation: str,
        query: str,
        variables: dict[str, Any],
        allow_partial_errors: bool = False,
    ) -> dict[str, Any]:
        self.query_counts[operation] = self.query_counts.get(operation, 0) + 1
        response = self.session.post(GRAPHQL_URL, json={"query": query, "variables": variables})
        if response.status_code == 403:
            raise RuntimeError("GitHub rejected the request due to its rate or abuse limit.")
        if response.status_code != 200:
            raise RuntimeError(
                f"{operation} failed with HTTP {response.status_code}: {response.text}"
            )
        payload = response.json()
        errors = payload.get("errors", [])
        if errors and not allow_partial_errors:
            raise RuntimeError(f"{operation} failed: {payload['errors']}")
        if errors:
            print(f"Warning: skipped inaccessible repository fields while calculating {operation}.")
        return payload["data"]

    @staticmethod
    def _next_cursor(page_info: dict[str, Any], cursor: str | None, operation: str) -> str | None:
        if not page_info.get("hasNextPage"):
            return None
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor == cursor:
            raise RuntimeError(f"{operation} returned a non-advancing pagination cursor.")
        return next_cursor

    def user(self) -> tuple[str, str]:
        data = self._request(
            "user_getter",
            "query($login: String!){ user(login: $login) { id createdAt } }",
            {"login": self.settings.username},
        )
        user = data["user"]
        return user["id"], user["createdAt"]

    def followers(self) -> int:
        data = self._request(
            "follower_getter",
            "query($login: String!){ user(login: $login) { followers { totalCount } } }",
            {"login": self.settings.username},
        )
        return int(data["user"]["followers"]["totalCount"])

    def repositories(self, affiliations: list[str]) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = self._request(
                "loc_query",
                REPOSITORY_QUERY,
                {"owner_affiliation": affiliations, "login": self.settings.username, "cursor": cursor},
            )
            page = data["user"]["repositories"]
            edges.extend(page["edges"])
            cursor = self._next_cursor(page["pageInfo"], cursor, "loc_query")
            if cursor is None:
                return edges

    def repository_totals(self, kind: str, affiliations: list[str]) -> int:
        if kind not in {"repos", "stars"}:
            raise ValueError(f"Unsupported repository total: {kind}")
        if kind == "stars":
            return self._star_total()
        cursor: str | None = None
        total = 0
        while True:
            data = self._request(
                "graph_repos_stars",
                REPOSITORIES_QUERY,
                {"owner_affiliation": affiliations, "login": self.settings.username, "cursor": cursor},
            )
            page = data["user"]["repositories"]
            total += page["totalCount"] if cursor is None else 0
            cursor = self._next_cursor(page["pageInfo"], cursor, "graph_repos_stars")
            if cursor is None:
                return total

    def _star_total(self) -> int:
        page = 1
        total = 0
        while True:
            self.query_counts["rest_stars"] = self.query_counts.get("rest_stars", 0) + 1
            response = self.session.get(
                "https://api.github.com/user/repos",
                params={"affiliation": "owner", "per_page": 100, "page": page},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"rest_stars failed with HTTP {response.status_code}: {response.text}"
                )
            repositories = response.json()
            total += sum(repository.get("stargazers_count", 0) for repository in repositories)
            if len(repositories) < 100:
                return total
            page += 1

    def commits_by_user(self, owner: str, repository: str, owner_id: str) -> tuple[int, int, int]:
        additions = deletions = authored = 0
        cursor: str | None = None
        while True:
            data = self._request(
                "recursive_loc",
                COMMIT_QUERY,
                {"repo_name": repository, "owner": owner, "cursor": cursor},
            )
            branch = data["repository"]["defaultBranchRef"]
            if branch is None:
                return additions, deletions, authored
            history = branch["target"]["history"]
            for edge in history["edges"]:
                node = edge["node"]
                if node["parents"]["totalCount"] > 1:
                    continue
                author = node["author"]["user"]
                if author is not None and author["id"] == owner_id:
                    authored += 1
                    additions += node["additions"]
                    deletions += node["deletions"]
            cursor = self._next_cursor(history["pageInfo"], cursor, "recursive_loc")
            if cursor is None:
                return additions, deletions, authored


class RepositoryCache:
    """Read, update, and persist the per-repository contribution cache."""

    def __init__(self, settings: Settings, client: GitHubClient, owner_id: str) -> None:
        self.settings = settings
        self.client = client
        self.owner_id = owner_id

    @staticmethod
    def _hash(name: str) -> str:
        return hashlib.sha256(name.encode("utf-8")).hexdigest()

    def calculate(self, repositories: list[dict[str, Any]], force: bool = False) -> list[int | bool]:
        path = self.settings.cache_file
        path.parent.mkdir(parents=True, exist_ok=True)
        comments, old_records = self._read(path)
        records = {record[0]: record for record in old_records if len(record) >= 5}
        cached = not force and len(records) == len(repositories)
        new_records: list[str] = []
        for edge in repositories:
            node = edge["node"]
            name = node["nameWithOwner"]
            repo_hash = self._hash(name)
            history = node["defaultBranchRef"]
            commit_count = (
                history["target"]["history"]["totalCount"] if history is not None else 0
            )
            record = records.get(repo_hash)
            if force or record is None or int(record[1]) != commit_count:
                if "/" not in name:
                    raise RuntimeError(f"Unexpected repository name: {name}")
                owner, repository = name.split("/", 1)
                additions, deletions, authored = self.client.commits_by_user(
                    owner, repository, self.owner_id
                )
                record = [repo_hash, str(commit_count), str(authored), str(additions), str(deletions)]
                cached = False
            new_records.append(" ".join(record) + "\n")
        path.write_text("".join(comments) + "".join(new_records), encoding="utf-8")
        additions = sum(int(line.split()[3]) for line in new_records)
        deletions = sum(int(line.split()[4]) for line in new_records)
        return [additions, deletions, additions - deletions, cached]

    def _read(self, path: Path) -> tuple[list[str], list[list[str]]]:
        if not path.exists():
            comments = self._new_comments()
            path.write_text("".join(comments), encoding="utf-8")
            return comments, []
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines or lines[0].strip() != self.settings.cache_version:
            return self._new_comments(), []
        return lines[: self.settings.comment_size], [
            line.split() for line in lines[self.settings.comment_size :] if line.split()
        ]

    def _new_comments(self) -> list[str]:
        return [
            f"{self.settings.cache_version}\n",
            *(
                ["This line is a comment block. Write whatever you want here.\n"]
                * (self.settings.comment_size - 1)
            ),
        ]

    def commit_count(self) -> int:
        _, records = self._read(self.settings.cache_file)
        return sum(int(record[2]) for record in records if len(record) >= 3)

    def report_largest_changes(self, repositories: list[dict[str, Any]], limit: int = 5) -> None:
        _, records = self._read(self.settings.cache_file)
        names = {
            self._hash(edge["node"]["nameWithOwner"]): edge["node"]["nameWithOwner"]
            for edge in repositories
        }
        largest_additions = sorted(records, key=lambda record: int(record[3]), reverse=True)[:limit]
        largest_deletions = sorted(records, key=lambda record: int(record[4]), reverse=True)[:limit]
        print("Largest historical additions (commit churn, not current LOC):")
        for record in largest_additions:
            print(f"   {names.get(record[0], '[unavailable repository]')}: {int(record[3]):,}")
        print("Largest historical deletions (commit churn, not current LOC):")
        for record in largest_deletions:
            print(f"   {names.get(record[0], '[unavailable repository]')}: {int(record[4]):,}")


class SvgWriter:
    @staticmethod
    def write(filename: str, age: str, commits: int, stars: int, repositories: int,
              contributions: int, followers: int, loc: list[int | str]) -> None:
        tree = etree.parse(filename)
        root = tree.getroot()
        for element_id, value, width in (
            ("commit_data", commits, 22), 
            ("star_data", stars, 14),
            ("repo_data", repositories, 7), 
            ("contrib_data", contributions, 0),
            ("follower_data", followers, 10), 
            ("loc_data", loc[2], 9),
            ("loc_add", loc[0], 0), 
            ("loc_del", loc[1], 7),
        ):
            formatted_value = f"{value:,}" if isinstance(value, int) else str(value)
            SvgWriter._replace(root, element_id, formatted_value)
            if width:
                remaining = max(0, width - len(formatted_value))
                dots = {0: "", 1: " ", 2: ". "}.get(remaining, f" {'.' * remaining} ")
                SvgWriter._replace(root, f"{element_id}_dots", dots)
        tree.write(filename, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _replace(root: Any, element_id: str, text: str) -> None:
        element = root.find(f".//*[@id='{element_id}']")
        if element is not None:
            element.text = text


def age_text(birthday: dt.datetime) -> str:
    difference = relativedelta.relativedelta(dt.datetime.today(), birthday)
    plural = lambda value: "" if value == 1 else "s"
    birthday_marker = " 🎂" if difference.months == 0 and difference.days == 0 else ""
    return (
        f"{difference.years} year{plural(difference.years)}, "
        f"{difference.months} month{plural(difference.months)}, "
        f"{difference.days} day{plural(difference.days)}{birthday_marker}"
    )


def archived_totals(path: Path) -> list[int]:
    if not path.exists():
        return [0, 0, 0, 0, 0]
    lines = path.read_text(encoding="utf-8").splitlines()
    records = lines[7:-3]
    additions = sum(int(line.split()[3]) for line in records)
    deletions = sum(int(line.split()[4]) for line in records)
    commits = sum(int(line.split()[2]) for line in records if line.split()[2].isdigit())
    return [additions, deletions, additions - deletions, commits, len(records)]


def timed(label: str, function: Any, *args: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    result = function(*args)
    elapsed = time.perf_counter() - started
    print(f"   {label + ':':<23}{elapsed * (1000 if elapsed <= 1 else 1):>10.4f} {'ms' if elapsed <= 1 else 's'}")
    return result, elapsed


def main() -> None:
    settings = Settings()
    if not settings.token: raise RuntimeError("Set ACCESS_TOKEN before running generate.py.")

    client = GitHubClient(settings)
    owner_id, _ = timed("account data", client.user)[0]
    age, age_time = timed("age calculation", age_text, dt.datetime(2004, 2, 16))
    repositories, _ = timed("LOC query", client.repositories, ["OWNER"])
    cache = RepositoryCache(settings, client, owner_id)
    loc, loc_time = timed("LOC (cached)", cache.calculate, repositories)
    cache.report_largest_changes(repositories)
    commits, commit_time = timed("commit count", cache.commit_count)
    stars, star_time = timed("stars", client.repository_totals, "stars", ["OWNER"])
    owned_repositories, repo_time = timed("repositories", client.repository_totals, "repos", ["OWNER"])
    contributions, contrib_time = timed("contributions", client.repository_totals, "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"])
    followers, follower_time = timed("followers", client.followers)

    archive = archived_totals(Path("cache/repository_archive.txt"))
    loc = [loc[index] + archive[index] for index in range(3)] + [loc[3]]
    commits += archive[3]
    contributions += archive[4]

    SvgWriter.write("about.svg", age, commits, stars, owned_repositories, contributions, followers, loc)

    total_time = sum((age_time, loc_time, commit_time, star_time, repo_time, contrib_time, follower_time))
    print(f"Total function time: {total_time:.4f} s")
    print("Total GitHub GraphQL API calls:", sum(client.query_counts.values()))
    for name, count in client.query_counts.items():
        print(f"   {name + ':':<25}{count:>6}")


if __name__ == "__main__":
    main()

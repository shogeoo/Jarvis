"""Read-only access to committed archives. Never expose unfinished staging."""

from pathlib import Path
from ..infrastructure.context import read_json, valid_name, hydrate_part, context_files
from .serialization import serialize_context, chunks


class ArchiveReader:
    def __init__(self, root, reranker):
        self.root = Path(root) / "sessions"
        self.reranker = reranker

    def directory(self, agent_id, session_id):
        if not valid_name(agent_id) or not valid_name(session_id):
            raise ValueError("Invalid archive identity")
        directory = self.root / session_id / "instances" / agent_id
        if (
            not (self.root / session_id / "summary.md").is_file()
            or not (directory / "context.json").is_file()
        ):
            raise ValueError("Completed session/instance not found")
        return directory

    def list_sessions(self, agent_id):
        if not valid_name(agent_id):
            raise ValueError("Invalid agent_id")
        return {
            "sessions": [
                p.name
                for p in sorted(self.root.glob("session_*"), reverse=True)
                if (p / "summary.md").is_file()
                and (p / "instances" / agent_id / "context.json").is_file()
            ]
        }

    def get_summary(self, agent_id, session_id, scope="instance"):
        directory = self.directory(agent_id, session_id)
        if scope not in {"instance", "session"}:
            raise ValueError("Invalid summary scope")
        path = directory if scope == "instance" else self.root / session_id
        return {"summary": (path / "summary.md").read_text(encoding="utf-8")}

    def get_context_info(self, agent_id, session_id):
        context = read_json(self.directory(agent_id, session_id) / "context.json")
        return {"messages": len(context), "characters": len(serialize_context(context, agent_id=agent_id, session_id=session_id))}

    def get_context(self, agent_id, session_id, skip_first=0, skip_last=0):
        if skip_first < 0 or skip_last < 0:
            raise ValueError("Message skips must be nonnegative")
        context = read_json(self.directory(agent_id, session_id) / "context.json")
        end = max(0, len(context) - skip_last)
        selected = context[skip_first:end]
        return {"context": serialize_context(selected, agent_id=agent_id, session_id=session_id), "total": len(selected)}

    def search(self, agent_id, query, limit=3):
        self._validate_search(query, limit)
        # Stream archives one at a time regardless of archive count.
        matches = []
        for session in self.list_sessions(agent_id)["sessions"]:
            directory = self.directory(agent_id, session)
            text = (directory / "summary.md").read_text(encoding="utf-8")
            score = self.reranker.score(query, [text])[0]
            metadata = read_json(directory / "instance.json")
            matches.append(
                {
                    "session_id": session,
                    "agent_id": agent_id,
                    "preset_id": metadata["preset_id"],
                    "score": score,
                }
            )
            matches.sort(key=lambda r: r["score"], reverse=True)
            del matches[limit:]
        return {"matches": matches}

    @staticmethod
    def _validate_search(query, limit):
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(limit, int)
            or limit < 1
        ):
            raise ValueError("Search requires a nonempty query and a positive limit")

    def search_in_context(self, agent_id, session_id, query, limit=3):
        self._validate_search(query, limit)
        context = read_json(self.directory(agent_id, session_id) / "context.json")
        candidates = chunks(
            context,
            self.reranker.tokenizer,
            max_tokens=self.reranker.candidate_capacity(query),
            agent_id=agent_id, session_id=session_id,
        )
        scores = (
            self.reranker.score(query, [c["text"] for c in candidates])
            if candidates
            else []
        )
        matches = [
            {"start": c["start"], "end": c["end"], "score": score}
            for c, score in zip(candidates, scores)
        ]
        return {
            "matches": sorted(matches, key=lambda r: r["score"], reverse=True)[:limit]
        }

    def get_file(self, agent_id, session_id, file_id):
        directory = self.directory(agent_id, session_id)
        files = context_files(read_json(directory / "context.json", []))
        part = next((item for item in files if item["file_id"] == file_id), None)
        if part is None:
            raise ValueError("Archived file not found")
        return part["filename"], hydrate_part(part, directory)

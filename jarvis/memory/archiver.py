"""Restartable summaries and completed-session archive commits."""
from __future__ import annotations

import json
import os
import shutil

from ..core.media import MediaInterpreter
from ..infrastructure.context import read_json, write_text, sync_directory
from ..infrastructure.prompt_templates import render_template
from .serialization import api_messages, summary_context


class Archiver(MediaInterpreter):
    def staging(self):
        session = self.store.state()["shutdown"]["session_id"]
        path = self.store.root / (".archive-" + session)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def summarize_instances(self):
        stage = self.staging()
        for record in self.store.records(include_deleted=True):
            self.check()
            agent = record["agent_id"]
            output = stage / "instances" / agent
            if (output / "summary.md").exists():
                continue
            directory = self.store.instances / agent
            context = read_json(directory / "context.json", [])
            history = api_messages(
                summary_context(context),
                directory,
                document_parts=lambda p: self.document_parts(p, directory),
                include_thinking=True,
            )
            media = [f for f in self.store.files(agent) if f["type"] != "file"]
            system = (
                "<PERSON>\n" + render_template("instance_summary_person.txt") + "\n</PERSON>"
                + "\n<INSTANCE_INFO>\n"
                + json.dumps({key: record[key] for key in
                    ("agent_id", "preset_id", "parent_id", "description", "deleted")
                    if key in record}, ensure_ascii=False)
                + "\n</INSTANCE_INFO>\n<ENVIRONMENT>\n"
                + self.environment
                + "\n</ENVIRONMENT>"
            )
            summary = self.request(
                [{"role": "system", "content": system}, *history],
                self.modalities(media, directory),
            )
            if output.exists():
                shutil.rmtree(output)
            shutil.copytree(
                directory,
                output,
                ignore=shutil.ignore_patterns(
                    "semantic.json", "files.json", "instance.json", ".save-journal.json", ".*.tmp"
                ),
            )
            self.store._write_json(output / "instance.json", {
                key: record.get(key) for key in
                ("agent_id", "preset_id", "parent_id", "description", "deleted")
                if key in record
            })
            write_text(output / "summary.md", summary)

    def summarize_session(self):
        stage = self.staging()
        if (stage / "summary.md").exists():
            return
        state = self.store.state()["shutdown"]
        instances = []
        for record in self.store.records(include_deleted=True):
            agent = record["agent_id"]
            instances.append(
                {
                    key: record.get(key)
                    for key in ("agent_id", "preset_id", "parent_id", "description")
                }
            )
            instances[-1]["summary"] = (
                stage / "instances" / agent / "summary.md"
            ).read_text(encoding="utf-8")
        prompt = (
            "<PERSON>\n" + render_template("session_summary_person.txt") + "\n</PERSON>"
            + "\n<ENVIRONMENT>\n" + self.environment + "\n</ENVIRONMENT>"
        )
        summary = self.request(
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"session_id": state["session_id"], "instances": instances},
                        ensure_ascii=False,
                    ),
                },
            ],
            {"text"},
        )
        write_text(stage / "summary.md", summary)

    def commit(self):
        self.check()
        session = self.store.state()["shutdown"]["session_id"]
        destination = self.store.sessions / session
        if destination.exists():
            return
        stage = self.staging()
        if not (stage / "summary.md").is_file():
            raise RuntimeError("Cannot commit an incomplete archive")
        # fsync every copied attachment/metadata file before publishing the directory.
        for path in stage.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for path in sorted((p for p in stage.rglob("*") if p.is_dir()), reverse=True):
            sync_directory(path)
        sync_directory(stage)
        self.store.sessions.mkdir(parents=True, exist_ok=True)
        os.replace(stage, destination)
        sync_directory(self.store.sessions)
        sync_directory(self.store.root)

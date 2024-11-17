#!/usr/bin/env python

import asyncio
import json
import socketio
from aider import models
from aider.coders import Coder
from aider.io import InputOutput, AutoCompleter
from aider.main import main as cli_main
import nest_asyncio
nest_asyncio.apply()

confirmation_result = None

async def run_editor_coder_stream(architect_coder, connector):
    # Use the editor_model from the main_model if it exists, otherwise use the main_model itself
    editor_model = architect_coder.main_model.editor_model or architect_coder.main_model

    kwargs = dict()
    kwargs["main_model"] = editor_model
    kwargs["edit_format"] = architect_coder.main_model.editor_edit_format
    kwargs["suggest_shell_commands"] = False
    kwargs["map_tokens"] = 0
    kwargs["total_cost"] = architect_coder.total_cost
    kwargs["cache_prompts"] = False
    kwargs["num_cache_warming_pings"] = 0
    kwargs["summarize_from_coder"] = False

    new_kwargs = dict(io=architect_coder.io, from_coder=architect_coder)
    new_kwargs.update(kwargs)

    editor_coder = Coder.create(**new_kwargs)
    editor_coder.cur_messages = []
    editor_coder.done_messages = []

    await connector.sio.emit('message', {
        "action": "response",
        "finished": False,
        "content": "\n\n"
    })

    for chunk in editor_coder.run_stream(architect_coder.partial_response_content):
        # add small sleeps here to allow other coroutines to run
        await connector.sio.emit('message', {
            "action": "response",
            "finished": False,
            "content": chunk
        })
        await asyncio.sleep(0.01)

class ConnectorInputOutput(InputOutput):
    def __init__(self, connector=None, **kwargs):
        super().__init__(**kwargs)
        self.connector = connector

    def confirm_ask(
            self,
            question,
            default="y",
            subject=None,
            explicit_yes_required=False,
            group=None,
            allow_never=False,
    ):
        if not self.connector:
            return False

        # Reset the result
        global confirmation_result
        confirmation_result = None

        # Create a new event loop for this thread if there isn't one
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        # Create coroutine for emitting the question
        async def ask_question():
            await self.connector.sio.emit('message', {
                'action': 'ask-question',
                'question': question,
                'subject': subject,
                'defaultAnswer': default
            })
            while confirmation_result is None:
                await asyncio.sleep(1)
            return confirmation_result

        task = loop.create_task(ask_question())
        try:
            result = loop.run_until_complete(task)
        except Exception as e:
            self.tool_output(f'EXCEPTION: {e}')
            return False

        if result == "y" and self.connector.running_coder and question == "Edit the files?":
            # Process architect coder
            task = loop.create_task(run_editor_coder_stream(self.connector.running_coder, self.connector))
            loop.run_until_complete(task)
            return False

        return result == "y"

def create_coder(connector):
    coder = cli_main(return_coder=True)
    if not isinstance(coder, Coder):
        raise ValueError(coder)
    if not coder.repo:
        raise ValueError("WebsocketConnector can currently only be used inside a git repo")

    io = ConnectorInputOutput(
        connector=connector,
        pretty=False,
        yes=None,
        input_history_file=coder.io.input_history_file,
        chat_history_file=coder.io.chat_history_file,
        input=coder.io.input,
        output=coder.io.output,
        user_input_color=coder.io.user_input_color,
        tool_output_color=coder.io.tool_output_color,
        tool_warning_color=coder.io.tool_warning_color,
        tool_error_color=coder.io.tool_error_color,
        completion_menu_color=coder.io.completion_menu_color,
        completion_menu_bg_color=coder.io.completion_menu_bg_color,
        completion_menu_current_color=coder.io.completion_menu_current_color,
        completion_menu_current_bg_color=coder.io.completion_menu_current_bg_color,
        assistant_output_color=coder.io.assistant_output_color,
        code_theme=coder.io.code_theme,
        dry_run=coder.io.dry_run,
        encoding=coder.io.encoding,
        llm_history_file=coder.io.llm_history_file,
        editingmode=coder.io.editingmode,
        fancy_input=False
    )
    coder.commands.io = io
    coder.io = io

    for line in coder.get_announcements():
        coder.io.tool_output(line)

    return coder

class Connector:
    def __init__(self, base_dir, server_url="http://localhost:24337"):
        self.base_dir = base_dir
        self.server_url = server_url

        self.coder = create_coder(self)
        self.coder.yield_stream = True
        self.coder.stream = True
        self.coder.pretty = False
        self.running_coder = None

        self.sio = socketio.AsyncClient()
        self._register_events()

    def _register_events(self):
        @self.sio.event
        async def connect():
            await self.on_connect()

        @self.sio.on("message")
        async def on_message(data):
            await self.on_message(data)

        @self.sio.event
        async def disconnect():
            await self.on_disconnect()

    async def on_connect(self):
        """Handle connection event."""
        self.coder.io.tool_output("CONNECTED TO SERVER")
        await self.sio.emit('message', {
            'action': 'init',
            'baseDir': self.base_dir,
            'listenTo': ['prompt', 'add-file', 'drop-file', 'answer-question']
        })
        await self.send_autocompletion()

    async def on_message(self, data):
        await self.process_message(data)

    async def on_disconnect(self):
        """Handle disconnection event."""
        self.coder.io.tool_output("DISCONNECTED FROM SERVER")

    async def send_message(self, message):
        """Send a message to the server."""
        await self.sio.send(message)

    async def connect(self):
        """Connect to the server."""
        await self.sio.connect(self.server_url)

    async def wait(self):
        """Wait for events."""
        await self.sio.wait()

    async def start(self):
        await self.connect()
        await self.wait()

    async def process_message(self, message):
        """Process incoming message and return response"""
        try:
            action = message.get('action')

            if not action:
                return json.dumps({"error": "No action specified"})

            if action == "prompt":
                prompt = message.get('prompt')
                edit_format = message.get('editFormat')
                if not prompt:
                    return
                elif prompt == "model":
                    self.coder = Coder.create(
                        from_coder=self.coder,
                        main_model=models.Model("deepseek/deepseek-coder")
                    )
                    for line in self.coder.get_announcements():
                        self.coder.io.tool_output(line)

                    return json.dumps({
                        "action": "response",
                        "content": "model switched",
                        "finished": True,
                        "editedFiles": [],
                    })

                self.coder.io.tool_output("> " + prompt)
                self.coder.io.add_to_input_history(prompt)

                self.running_coder = self.coder
                if edit_format:
                    self.running_coder = Coder.create(
                        from_coder=self.coder,
                        edit_format=edit_format,
                        summarize_from_coder=False
                    )

                whole_content = ""

                async def run_stream_async():
                    for chunk in self.running_coder.run_stream(prompt):
                        # add small sleeps here to allow other coroutines to run
                        await asyncio.sleep(0.01)
                        yield chunk

                async for chunk in run_stream_async():
                    whole_content += chunk
                    await self.sio.emit('message', {
                        "action": "response",
                        "finished": False,
                        "content": chunk
                    })

                # Send final response with complete data
                response_data = {
                    "action": "response",
                    "content": whole_content,
                    "finished": True,
                    "editedFiles": list(self.running_coder.aider_edited_files),
                }

                # Add commit info if there was one
                if self.running_coder.last_aider_commit_hash:
                    response_data.update({
                        "commitHash": self.running_coder.last_aider_commit_hash,
                        "commitMessage": self.running_coder.last_aider_commit_message,
                    })
                    # Add diff if there was a commit
                    commits = f"{self.running_coder.last_aider_commit_hash}~1"
                    diff = self.running_coder.repo.diff_commits(
                        self.running_coder.pretty,
                        commits,
                        self.running_coder.last_aider_commit_hash,
                    )
                    response_data["diff"] = diff

                if self.running_coder != self.coder:
                    self.coder = Coder.create(
                        edit_format=self.coder.edit_format,
                        summarize_from_coder=False,
                        from_coder=self.running_coder,
                    )

                await self.sio.emit('message', response_data)
                return

            elif action == "answer-question":
                global confirmation_result
                confirmation_result = message.get('answer')

            elif action == "add-file":
                path = message.get('path')
                if not path:
                    return

                await self.add_file(path)

            elif action == "drop-file":
                path = message.get('path')
                if not path:
                    return

                await self.drop_file(path)

            else:
                return json.dumps({
                    "error": f"Unknown action: {action}"
                })

        except Exception as e:
            return json.dumps({
                "error": str(e)
            })

    async def add_file(self, path):
        """Add a file to the coder's tracked files"""
        self.coder.add_rel_fname(path)
        await self.send_autocompletion()

    async def drop_file(self, path):
        """Drop a file from the coder's tracked files"""
        self.coder.drop_rel_fname(path)
        await self.send_autocompletion()

    async def send_autocompletion(self):
        inchat_files = self.coder.get_inchat_relative_files()
        read_only_files = [self.coder.get_rel_fname(fname) for fname in self.coder.abs_read_only_fnames]
        rel_fnames = sorted(set(inchat_files + read_only_files))
        auto_completer = AutoCompleter(
            root=self.coder.root,
            rel_fnames=rel_fnames,
            addable_rel_fnames=self.coder.get_addable_relative_files(),
            commands=None,
            encoding=self.coder.io.encoding,
            abs_read_only_fnames=self.coder.abs_read_only_fnames,
        )
        auto_completer.tokenize()

        words = [word[0] if isinstance(word, tuple) else word for word in auto_completer.words]
        words = list(words) + [fname.split('/')[-1] for fname in rel_fnames]

        if self.sio:
            await self.sio.emit("message", {
                "action": "update-autocompletion",
                "words": words,
                "allFiles": self.coder.get_all_relative_files()
            })

def main():
    import sys
    base_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    connector = Connector(base_dir)
    connector.start()


if __name__ == "__main__":
    main()

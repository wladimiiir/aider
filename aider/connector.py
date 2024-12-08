#!/usr/bin/env python

import asyncio
import json
import socketio
from aider import models
from aider.coders import Coder
from aider.io import InputOutput, AutoCompleter
from aider.watch import FileWatcher
from aider.main import main as cli_main
from aider.utils import is_image_file
import nest_asyncio
nest_asyncio.apply()

confirmation_result = None

def wait_for_async(connector, coroutine):
    try:
        task = connector.loop.create_task(coroutine)
        result = connector.loop.run_until_complete(task)
        return result
    except Exception as e:
        connector.coder.io.tool_output(f'EXCEPTION: {e}')
        return None

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
        self.running_shell_command = False
        self.current_command = None

    def tool_output(self, *messages, log_only=False, bold=False):
        super().tool_output(*messages, log_only=log_only, bold=bold)
        if self.running_shell_command:
            for message in messages:
                # Extract current command from "Running" messages
                if message.startswith("Running ") and not self.current_command:
                    async def send_use_command_output():
                        await self.connector.send_action({
                            "action": "use-command-output",
                            "command": self.current_command,
                        })
                        await asyncio.sleep(0.1)

                    self.current_command = message[8:]
                    wait_for_async(self.connector, send_use_command_output())

    def is_warning_ignored(self, message):
        return False

    def tool_warning(self, message="", strip=True):
        super().tool_warning(message, strip)
        if self.connector and not self.is_warning_ignored(message):
            wait_for_async(self.connector, self.connector.send_log_message("warning", message))

    def is_error_ignored(self, message):
        if message.endswith("is already in the chat as a read-only file"):
            return True
        if message.endswith("is already in the chat as an editable file"):
            return True

        return False

    def tool_error(self, message="", strip=True):
        super().tool_error(message, strip)
        if self.connector and not self.is_error_ignored(message):
            wait_for_async(self.connector, self.connector.send_log_message("error", message))
    
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

        result = wait_for_async(self.connector, ask_question())

        if result == "y" and self.connector.running_coder and question == "Edit the files?":
            # Process architect coder
            wait_for_async(self.connector, run_editor_coder_stream(self.connector.running_coder, self.connector))
            return False

        if result == "y" and question.startswith("Run shell command"):
            self.running_shell_command = True
            self.current_command = None
        if question == "Add command output to the chat?":
            self.reset_state()

        return result == "y"

    def reset_state(self):
        if (self.current_command):
            wait_for_async(self.connector, self.connector.send_action({
                "action": "use-command-output",
                "command": self.current_command,
                "finished": True
            }))

            self.running_shell_command = False
            self.current_command = None

    def interrupt_input(self):
        async def process_changes():
            await self.connector.run_prompt(prompt)
            await self.connector.send_update_context_files()
            self.connector.file_watcher.start()
        
        if self.connector.file_watcher:
            prompt = self.connector.file_watcher.process_changes()
            if prompt:
                changed_files = ", ".join(sorted(self.connector.file_watcher.changed_files))
                wait_for_async(self.connector, self.connector.send_log_message("info", f"Detected changes in files: {changed_files}."))
                wait_for_async(self.connector, self.connector.send_action({
                    "action": "response",
                    "finished": False,
                    "content": ""
                }))
                self.connector.loop.create_task(process_changes())

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

    return coder

class Connector:
    def __init__(self, base_dir, watch_files=False, server_url="http://localhost:24337"):
        self.base_dir = base_dir
        self.server_url = server_url

        self.coder = create_coder(self)
        self.coder.yield_stream = True
        self.coder.stream = True
        self.coder.pretty = False
        self.running_coder = None
        
        if watch_files:
            self.file_watcher = FileWatcher(self.coder)
            self.file_watcher.start()

        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

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
        await self.send_action({
            'action': 'init',
            'baseDir': self.base_dir,
            'listenTo': ['prompt', 'add-file', 'drop-file', 'answer-question', 'set-models', 'run-command']
        })
        await self.send_autocompletion()
        await self.send_current_models()

    async def on_message(self, data):
        await self.process_message(data)

    async def on_disconnect(self):
        """Handle disconnection event."""
        self.coder.io.tool_output("DISCONNECTED FROM SERVER")

    async def connect(self):
        """Connect to the server."""
        await self.sio.connect(self.server_url)

    async def wait(self):
        """Wait for events."""
        await self.sio.wait()

    async def start(self):
        await self.connect()
        await self.wait()

    async def send_action(self, action, with_delay = True):
        await self.sio.emit('message', action)
        if with_delay:
            await asyncio.sleep(0.01)

    async def send_log_message(self, level, message):
        self.coder.io.tool_output(f"Sending {level} message to server... {message}")
        await self.sio.emit("log", {
            'level': level,
            'message': message
        })
        await asyncio.sleep(0.01)

    async def process_message(self, message):
        """Process incoming message and return response"""
        try:
            action = message.get('action')

            if not action:
                return json.dumps({"error": "No action specified"})

            self.reset_before_action()

            if action == "prompt":
                prompt = message.get('prompt')
                edit_format = message.get('editFormat')
                if not prompt:
                    return
                
                await self.run_prompt(prompt, edit_format)

            elif action == "answer-question":
                global confirmation_result
                confirmation_result = message.get('answer')

            elif action == "add-file":
                path = message.get('path')
                if not path:
                    return

                read_only = message.get('readOnly')
                await self.add_file(path, read_only)

            elif action == "drop-file":
                path = message.get('path')
                if not path:
                    return

                await self.drop_file(path)

            elif action == "set-models":
                model_name = message.get('name')
                if not model_name:
                    return

                main_model = models.Model(model_name)
                models.sanity_check_models(self.coder.io, main_model)
                
                self.coder = Coder.create(
                    from_coder=self.coder,
                    main_model=main_model
                )
                for line in self.coder.get_announcements():
                    self.coder.io.tool_output(line)
                await self.send_current_models()

            elif action == "run-command":
                command = message.get('command')
                if not command:
                    return

                self.coder.commands.run(command)
                if command.startswith("/paste"):
                    await asyncio.sleep(0.1)
                    await self.send_update_context_files()
                elif command.startswith("/clear"):
                    await asyncio.sleep(0.1)
                    await self.send_tokens_info()

            else:
                return json.dumps({
                    "error": f"Unknown action: {action}"
                })

        except Exception as e:
            self.coder.io.tool_error(f"Exception in connector: {str(e)}")
            return json.dumps({
                "error": str(e)
            })
        
    def reset_before_action(self):
        self.coder.io.reset_state()

    async def run_prompt(self, prompt, edit_format=None):
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
            try:
                for chunk in self.running_coder.run_stream(prompt):
                    # add small sleeps here to allow other coroutines to run
                    await asyncio.sleep(0.01)
                    yield chunk
            except Exception as e:
                self.coder.io.tool_error(str(e))

        async for chunk in run_stream_async():
            whole_content += chunk
            await self.send_action({
                "action": "response",
                "finished": False,
                "content": chunk
            }, False)

        # Send final response with complete data
        response_data = {
            "action": "response",
            "content": whole_content,
            "finished": True,
            "editedFiles": list(self.running_coder.aider_edited_files),
            "usageReport": self.running_coder.usage_report
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

        await self.send_action(response_data)

        if self.running_coder != self.coder:
            self.coder = Coder.create(
                edit_format=self.coder.edit_format,
                summarize_from_coder=False,
                from_coder=self.running_coder,
            )
        await self.send_update_context_files()

        # Check for reflections
        if self.running_coder.reflected_message:
            current_reflection = 0
            while self.running_coder.reflected_message:
                prompt = self.running_coder.reflected_message

                # use default coder to run the reflection
                self.running_coder = self.coder
                whole_content = ""
                async for chunk in run_stream_async():
                    whole_content += chunk
                    await self.send_action({
                        "action": "response",
                        "reflectedMessage": prompt,
                        "finished": False,
                        "content": chunk
                    }, False)

                response_data = {
                    "action": "response",
                    "content": whole_content,
                    "reflected_message": prompt,
                    "finished": True,
                    "editedFiles": list(self.running_coder.aider_edited_files),
                    "usageReport": self.running_coder.usage_report
                }
                await self.send_action(response_data)
                await self.send_update_context_files()
                if current_reflection >= self.coder.max_reflections:
                    self.coder.io.tool_warning(f"Only {str(self.coder.max_reflections)} reflections allowed, stopping.")
                    break
                current_reflection += 1
        
        self.running_coder = None
        await self.send_autocompletion()
        await self.send_tokens_info()

    async def add_file(self, path, read_only):
        """Add a file to the coder's tracked files"""
        if read_only:
            self.coder.commands.cmd_read_only("\"" + path + "\"")
        else:
            self.coder.commands.cmd_add("\"" + path + "\"")
        await self.send_update_context_files()
        await self.send_autocompletion()
        await self.send_tokens_info()

    async def drop_file(self, path):
        """Drop a file from the coder's tracked files"""
        self.coder.commands.cmd_drop("\"" + path + "\"")
        await self.send_update_context_files()
        await self.send_autocompletion()
        await self.send_tokens_info()

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
                "allFiles": self.coder.get_all_relative_files(),
                "models": models.fuzzy_match_models("")
            })

    async def send_update_context_files(self):
        if self.sio:
            inchat_files = self.coder.get_inchat_relative_files()
            read_only_files = [self.coder.get_rel_fname(fname) for fname in self.coder.abs_read_only_fnames]
            
            context_files = [
                {"path": fname, "readOnly": False} for fname in inchat_files
            ] + [
                {"path": fname, "readOnly": True} for fname in read_only_files
            ]
            
            await self.sio.emit("message", {
                "action": "update-context-files",
                "files": context_files
            })

    async def send_current_models(self):
        if self.sio:
            error = None
            info = self.coder.main_model.info

            if self.coder.main_model.missing_keys:
                error = "Missing keys for the model: " + ", ".join(self.coder.main_model.missing_keys)
            if not info:
                error = "Model is not available. Try setting a different model."
                possible_matches = models.fuzzy_match_models(self.coder.main_model.name)
                if possible_matches:
                    error += "\nDid you mean one of these?"
                    for match in possible_matches:
                        error += f"\n- {match}"

            await self.sio.emit("message", {
                "action": "set-models",
                "name": self.coder.main_model.name,
                "weakModel": self.coder.main_model.weak_model.name,
                "maxChatHistoryTokens": self.coder.main_model.max_chat_history_tokens,
                "info": info,
                "error": error
            })
    
    async def send_tokens_info(self):
        cost_per_token = self.coder.main_model.info.get("input_cost_per_token") or 0
        info = {
            "files": {}
        }
        
        self.coder.choose_fence()

        # system messages
        main_sys = self.coder.fmt_system_prompt(self.coder.gpt_prompts.main_system)
        main_sys += "\n" + self.coder.fmt_system_prompt(self.coder.gpt_prompts.system_reminder)
        msgs = [
            dict(role="system", content=main_sys),
            dict(
                role="system",
                content=self.coder.fmt_system_prompt(self.coder.gpt_prompts.system_reminder),
            ),
        ]
        tokens = self.coder.main_model.token_count(msgs)
        info["systemMessages"] = {
            "tokens": tokens,
            "cost": tokens * cost_per_token,
        }

        # chat history
        msgs = self.coder.done_messages + self.coder.cur_messages
        if msgs:
            tokens = self.coder.main_model.token_count(msgs)
        else:
            tokens = 0
        info["chatHistory"] = {
            "tokens": tokens,
            "cost": tokens * cost_per_token,
        }

        # repo map
        other_files = set(self.coder.get_all_abs_files()) - set(self.coder.abs_fnames)
        if self.coder.repo_map:
            repo_content = self.coder.repo_map.get_repo_map(self.coder.abs_fnames, other_files)
            if repo_content:
                tokens = self.coder.main_model.token_count(repo_content)
            else:
                tokens = 0
        else:
            tokens = 0
        info["repoMap"] = {
            "tokens": tokens,
            "cost": tokens * cost_per_token,
        }

        fence = "`" * 3

        # files
        for fname in self.coder.abs_fnames:
            relative_fname = self.coder.get_rel_fname(fname)
            content = self.coder.io.read_text(fname)
            if is_image_file(relative_fname):
                tokens = self.coder.main_model.token_count_for_image(fname)
            else:
                # approximate
                content = f"{relative_fname}\n{fence}\n" + content + "{fence}\n"
                tokens = self.coder.main_model.token_count(content)
            info["files"][relative_fname] = {
                "tokens": tokens,
                "cost": tokens * cost_per_token,
            }

        # read-only files
        for fname in self.coder.abs_read_only_fnames:
            relative_fname = self.coder.get_rel_fname(fname)
            content = self.coder.io.read_text(fname)
            if content is not None and not is_image_file(relative_fname):
                # approximate
                content = f"{relative_fname}\n{fence}\n" + content + "{fence}\n"
                tokens = self.coder.main_model.token_count(content)
                info["files"][relative_fname] = {
                    "tokens": tokens,
                    "cost": tokens * cost_per_token,
                }

        if self.sio:
            await self.sio.emit("message", {
                "action": "tokens-info",
                "info": info
            })

def main():
    import sys
    base_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    connector = Connector(base_dir)
    connector.start()


if __name__ == "__main__":
    main()

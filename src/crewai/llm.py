import json
import logging
import os
import sys
import threading
import warnings
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Union, cast

from dotenv import load_dotenv

with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)
    import litellm
    from litellm import get_supported_openai_params
    from litellm import Choices, get_supported_openai_params
    from litellm.types.utils import ModelResponse


from crewai.utilities.exceptions.context_window_exceeding_exception import (
    LLMContextLengthExceededException,
)

load_dotenv()


class FilteredStream:
    def __init__(self, original_stream):
        self._original_stream = original_stream
        self._lock = threading.Lock()

    def write(self, s) -> int:
        with self._lock:
            # Filter out extraneous messages from LiteLLM
            if (
                "Give Feedback / Get Help: https://github.com/BerriAI/litellm/issues/new"
                in s
                or "LiteLLM.Info: If you need to debug this error, use `litellm.set_verbose=True`"
                in s
            ):
                return 0
            return self._original_stream.write(s)

    def flush(self):
        with self._lock:
            return self._original_stream.flush()


LLM_CONTEXT_WINDOW_SIZES = {
    # openai
    "gpt-4": 8192,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4-turbo": 128000,
    "o1-preview": 128000,
    "o1-mini": 128000,
    # gemini
    "gemini-2.0-flash": 1048576,
    "gemini-1.5-pro": 2097152,
    "gemini-1.5-flash": 1048576,
    "gemini-1.5-flash-8b": 1048576,
    # deepseek
    "deepseek-chat": 128000,
    # groq
    "gemma2-9b-it": 8192,
    "gemma-7b-it": 8192,
    "llama3-groq-70b-8192-tool-use-preview": 8192,
    "llama3-groq-8b-8192-tool-use-preview": 8192,
    "llama-3.1-70b-versatile": 131072,
    "llama-3.1-8b-instant": 131072,
    "llama-3.2-1b-preview": 8192,
    "llama-3.2-3b-preview": 8192,
    "llama-3.2-11b-text-preview": 8192,
    "llama-3.2-90b-text-preview": 8192,
    "llama3-70b-8192": 8192,
    "llama3-8b-8192": 8192,
    "mixtral-8x7b-32768": 32768,
    "llama-3.3-70b-versatile": 128000,
    "llama-3.3-70b-instruct": 128000,
}

DEFAULT_CONTEXT_WINDOW_SIZE = 8192
CONTEXT_WINDOW_USAGE_RATIO = 0.75


@contextmanager
def suppress_warnings():
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore")

        # Redirect stdout and stderr
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = FilteredStream(old_stdout)
        sys.stderr = FilteredStream(old_stderr)
        try:
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


class LLM:
    def __init__(
        self,
        model: str,
        timeout: Optional[Union[float, int]] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        n: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        max_completion_tokens: Optional[int] = None,
        max_tokens: Optional[int] = None,
        presence_penalty: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        logit_bias: Optional[Dict[int, float]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        seed: Optional[int] = None,
        logprobs: Optional[bool] = None,
        top_logprobs: Optional[int] = None,
        base_url: Optional[str] = None,
        api_version: Optional[str] = None,
        api_key: Optional[str] = None,
        callbacks: List[Any] = [],
    ):
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self.n = n
        self.stop = stop
        self.max_completion_tokens = max_completion_tokens
        self.max_tokens = max_tokens
        self.presence_penalty = presence_penalty
        self.frequency_penalty = frequency_penalty
        self.logit_bias = logit_bias
        self.response_format = response_format
        self.seed = seed
        self.logprobs = logprobs
        self.top_logprobs = top_logprobs
        self.base_url = base_url
        self.api_version = api_version
        self.api_key = api_key
        self.callbacks = callbacks
        self.context_window_size = 0

        # For safety, we disable passing init params to next calls
        litellm.drop_params = True

        self.set_callbacks(callbacks)
        self.set_env_callbacks()

    def to_dict(self) -> dict:
        """
        Return a dict of all relevant parameters for serialization.
        """
        return {
            "model": self.model,
            "timeout": self.timeout,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "n": self.n,
            "stop": self.stop,
            "max_completion_tokens": self.max_completion_tokens,
            "max_tokens": self.max_tokens,
            "presence_penalty": self.presence_penalty,
            "frequency_penalty": self.frequency_penalty,
            "logit_bias": self.logit_bias,
            "response_format": self.response_format,
            "seed": self.seed,
            "logprobs": self.logprobs,
            "top_logprobs": self.top_logprobs,
            "base_url": self.base_url,
            "api_version": self.api_version,
            "api_key": self.api_key,
            "callbacks": self.callbacks,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LLM":
        """
        Create an LLM instance from a dict.
        We assume the dict has all relevant keys that match what's in the constructor.
        """
        known_fields = {}
        known_fields["model"] = data.pop("model", None)
        known_fields["timeout"] = data.pop("timeout", None)
        known_fields["temperature"] = data.pop("temperature", None)
        known_fields["top_p"] = data.pop("top_p", None)
        known_fields["n"] = data.pop("n", None)
        known_fields["stop"] = data.pop("stop", None)
        known_fields["max_completion_tokens"] = data.pop("max_completion_tokens", None)
        known_fields["max_tokens"] = data.pop("max_tokens", None)
        known_fields["presence_penalty"] = data.pop("presence_penalty", None)
        known_fields["frequency_penalty"] = data.pop("frequency_penalty", None)
        known_fields["logit_bias"] = data.pop("logit_bias", None)
        known_fields["response_format"] = data.pop("response_format", None)
        known_fields["seed"] = data.pop("seed", None)
        known_fields["logprobs"] = data.pop("logprobs", None)
        known_fields["top_logprobs"] = data.pop("top_logprobs", None)
        known_fields["base_url"] = data.pop("base_url", None)
        known_fields["api_version"] = data.pop("api_version", None)
        known_fields["api_key"] = data.pop("api_key", None)
        known_fields["callbacks"] = data.pop("callbacks", None)

        return cls(**known_fields, **data)

    def call(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]] = None,
        callbacks: Optional[List[Any]] = None,
        available_functions: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        High-level call method that:
          1) Calls litellm.completion
          2) Checks for function/tool calls
          3) If a tool call is found:
               a) executes the function
               b) returns the result
          4) If no tool call, returns the text response

        :param messages: The conversation messages
        :param tools: Optional list of function schemas for function calling
        :param callbacks: Optional list of callbacks
        :param available_functions: A dictionary mapping function_name -> actual Python function
        :return: Final text response from the LLM or the tool result
        """
        with suppress_warnings():
            if callbacks:
                self.set_callbacks(callbacks)

            try:
                # --- 1) Make the completion call
                params = {
                    "model": self.model,
                    "messages": messages,
                    "timeout": self.timeout,
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "n": self.n,
                    "stop": self.stop,
                    "max_tokens": self.max_tokens or self.max_completion_tokens,
                    "presence_penalty": self.presence_penalty,
                    "frequency_penalty": self.frequency_penalty,
                    "logit_bias": self.logit_bias,
                    "response_format": self.response_format,
                    "seed": self.seed,
                    "logprobs": self.logprobs,
                    "top_logprobs": self.top_logprobs,
                    "api_base": self.base_url,
                    "api_version": self.api_version,
                    "api_key": self.api_key,
                    "stream": False,
                    "tools": tools,  # pass the tool schema
                }

                # Remove None values
                params = {k: v for k, v in params.items() if v is not None}

                response = litellm.completion(**params)
                response_message = cast(Choices, cast(ModelResponse, response).choices)[
                    0
                ].message
                text_response = response_message.content or ""
                tool_calls = getattr(response_message, "tool_calls", [])

                # --- 2) If no tool calls, return the text response
                if not tool_calls or not available_functions:
                    return text_response

                # --- 3) Handle the tool call
                tool_call = tool_calls[0]
                function_name = tool_call.function.name

                if function_name in available_functions:
                    # Parse arguments
                    try:
                        function_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError as e:
                        logging.warning(f"Failed to parse function arguments: {e}")
                        return text_response  # Fallback to text response

                    fn = available_functions[function_name]
                    try:
                        # Call the actual tool function
                        result = fn(**function_args)

                        print(f"Result from function '{function_name}': {result}")

                        # Return the result directly
                        return result

                    except Exception as e:
                        logging.error(
                            f"Error executing function '{function_name}': {e}"
                        )
                        return text_response  # Fallback to text response

                else:
                    logging.warning(
                        f"Tool call requested unknown function '{function_name}'"
                    )
                    return text_response  # Fallback to text response

            except Exception as e:
                # Check if context length was exceeded, otherwise log
                if not LLMContextLengthExceededException(
                    str(e)
                )._is_context_limit_error(str(e)):
                    logging.error(f"LiteLLM call failed: {str(e)}")
                # Re-raise the exception
                raise

    def supports_function_calling(self) -> bool:
        try:
            params = get_supported_openai_params(model=self.model)
            return "response_format" in params
        except Exception as e:
            logging.error(f"Failed to get supported params: {str(e)}")
            return False

    def supports_stop_words(self) -> bool:
        try:
            params = get_supported_openai_params(model=self.model)
            return "stop" in params
        except Exception as e:
            logging.error(f"Failed to get supported params: {str(e)}")
            return False

    def get_context_window_size(self) -> int:
        """
        Returns the context window size, using 75% of the maximum to avoid
        cutting off messages mid-thread.
        """
        if self.context_window_size != 0:
            return self.context_window_size

        self.context_window_size = int(
            DEFAULT_CONTEXT_WINDOW_SIZE * CONTEXT_WINDOW_USAGE_RATIO
        )
        for key, value in LLM_CONTEXT_WINDOW_SIZES.items():
            if self.model.startswith(key):
                self.context_window_size = int(value * CONTEXT_WINDOW_USAGE_RATIO)
        return self.context_window_size

    def set_callbacks(self, callbacks: List[Any]):
        """
        Attempt to keep a single set of callbacks in litellm by removing old
        duplicates and adding new ones.
        """
        callback_types = [type(callback) for callback in callbacks]
        for callback in litellm.success_callback[:]:
            if type(callback) in callback_types:
                litellm.success_callback.remove(callback)

        for callback in litellm._async_success_callback[:]:
            if type(callback) in callback_types:
                litellm._async_success_callback.remove(callback)

        litellm.callbacks = callbacks

    def set_env_callbacks(self):
        """
        Sets the success and failure callbacks for the LiteLLM library from environment variables.
        """
        success_callbacks_str = os.environ.get("LITELLM_SUCCESS_CALLBACKS", "")
        success_callbacks = []
        if success_callbacks_str:
            success_callbacks = [
                cb.strip() for cb in success_callbacks_str.split(",") if cb.strip()
            ]

        failure_callbacks_str = os.environ.get("LITELLM_FAILURE_CALLBACKS", "")
        failure_callbacks = []
        if failure_callbacks_str:
            failure_callbacks = [
                cb.strip() for cb in failure_callbacks_str.split(",") if cb.strip()
            ]

        litellm.success_callback = success_callbacks
        litellm.failure_callback = failure_callbacks

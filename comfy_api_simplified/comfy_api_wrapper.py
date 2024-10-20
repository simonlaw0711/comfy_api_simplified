import json
import requests
import uuid
import logging
import websockets
import asyncio
from requests.auth import HTTPBasicAuth
from requests.compat import urljoin, urlencode
from comfy_api_simplified.comfy_workflow_wrapper import ComfyWorkflowWrapper
import os

_log = logging.getLogger(__name__)


class ComfyApiWrapper:
    def __init__(
        self, url: str = "http://127.0.0.1:8188", user: str = "", password: str = ""
    ):
        """
        Initializes the ComfyApiWrapper object.

        Args:
            url (str): The URL of the Comfy API server. Defaults to "http://127.0.0.1:8188".
            user (str): The username for authentication. Defaults to an empty string.
            password (str): The password for authentication. Defaults to an empty string.
        """
        self.url = url
        self.auth = None
        url_without_protocol = url.split("//")[-1]

        if "https" in url:
            ws_protocol = "wss"
        else:
            ws_protocol = "ws"

        if user:
            self.auth = HTTPBasicAuth(user, password)
            ws_url_base = f"{ws_protocol}://{user}:{password}@{url_without_protocol}"
        else:
            ws_url_base = f"{ws_protocol}://{url_without_protocol}"
        self.ws_url = urljoin(ws_url_base, "/ws?clientId={}")

    def queue_prompt(self, prompt: dict, client_id: str | None = None) -> dict:
        """
        Queues a prompt for execution.

        Args:
            prompt (dict): The prompt to be executed.
            client_id (str): The client ID for the prompt. Defaults to None.

        Returns:
            dict: The response JSON object.

        Raises:
            Exception: If the request fails with a non-200 status code.
        """
        p = {"prompt": prompt}
        if client_id:
            p["client_id"] = client_id
        data = json.dumps(p).encode("utf-8")
        _log.info(f"Posting prompt to {self.url}/prompt")
        resp = requests.post(urljoin(self.url, "/prompt"), data=data, auth=self.auth)
        _log.info(f"{resp.status_code}: {resp.reason}")
        if resp.status_code == 200:
            return resp.json()
        else:
            raise Exception(
                f"Request failed with status code {resp.status_code}: {resp.reason}"
            )

    async def queue_prompt_and_wait(self, prompt: dict) -> str:
        """
        Queues a prompt for execution and waits for the result.

        Args:
            prompt (dict): The prompt to be executed.

        Returns:
            str: The prompt ID.

        Raises:
            Exception: If an execution error occurs.
        """
        client_id = str(uuid.uuid4())
        resp = self.queue_prompt(prompt, client_id)
        _log.debug(resp)
        prompt_id = resp["prompt_id"]
        _log.info(f"Connecting to {self.ws_url.format(client_id).split('@')[-1]}")
        
        # Increase timeout to 5 minutes (300 seconds)
        async with websockets.connect(
            uri=self.ws_url.format(client_id),
            ping_interval=None,
            ping_timeout=300
        ) as websocket:
            while True:
                try:
                    out = await asyncio.wait_for(websocket.recv(), timeout=300)
                    if isinstance(out, str):
                        message = json.loads(out)
                        if message["type"] == "crystools.monitor":
                            continue
                        _log.debug(message)
                        if message["type"] == "execution_error":
                            data = message["data"]
                            if data["prompt_id"] == prompt_id:
                                raise Exception("Execution error occurred.")
                        if message["type"] == "status":
                            data = message["data"]
                            if data["status"]["exec_info"]["queue_remaining"] == 0:
                                return prompt_id
                        if message["type"] == "executing":
                            data = message["data"]
                            if data["node"] is None and data["prompt_id"] == prompt_id:
                                return prompt_id
                except asyncio.TimeoutError:
                    print("Connection timed out. Retrying...")
                    continue

    def queue_and_wait_images(
        self, prompt: ComfyWorkflowWrapper, output_node_title: str
    ) -> dict:
        """
        Queues a prompt with a ComfyWorkflowWrapper object and waits for the images to be generated.

        Args:
            prompt (ComfyWorkflowWrapper): The ComfyWorkflowWrapper object representing the prompt.
            output_node_title (str): The title of the output node.

        Returns:
            dict: A dictionary mapping image filenames to their content.

        Raises:
            Exception: If the request fails with a non-200 status code.
        """
        loop = asyncio.get_event_loop()
        prompt_id = loop.run_until_complete(self.queue_prompt_and_wait(prompt.get_prompt()))
        history = self.get_history(prompt_id)
        output_node_id = prompt.get_node_id(output_node_title)
        
        print(f"Prompt ID: {prompt_id}")
        print(f"Output Node ID: {output_node_id}")
        print(f"All node IDs: {prompt.get_node_ids()}")
        
        if prompt_id not in history:
            raise KeyError(f"Prompt ID {prompt_id} not found in history")
        
        if "outputs" not in history[prompt_id]:
            raise KeyError(f"No outputs found for prompt ID {prompt_id}")
        
        if output_node_id not in history[prompt_id]["outputs"]:
            raise KeyError(f"Output node {output_node_id} not found in history")
        
        output_data = history[prompt_id]["outputs"][output_node_id]
        
        if "images" in output_data:
            return {
                image["filename"]: self.get_image(
                    image["filename"], image["subfolder"], image["type"]
                )
                for image in output_data["images"]
            }
        elif "gifs" in output_data:
            return {
                gif["filename"]: self.get_image(
                    gif["filename"], gif["subfolder"], gif["type"]
                )
                for gif in output_data["gifs"]
            }
        elif "videos" in output_data:
            return {
                video["filename"]: self.get_image(
                    video["filename"], video["subfolder"], video["type"]
                )
                for video in output_data["videos"]
            }
        else:
            raise KeyError(f"No images, gifs, or videos found in output node {output_node_id}")

    def get_queue(self) -> dict:
        """
        Retrieves the entire prompt queue.

        Returns:
            dict: The response JSON object.

        Raises:
            Exception: If the request fails with a non-200 status code.
        """
        url = urljoin(self.url, f"/queue")
        _log.info(f"Getting queue from {url}")
        resp = requests.get(url, auth=self.auth)
        if resp.status_code == 200:
            return resp.json()
        else:
            raise Exception(
                f"Request failed with status code {resp.status_code}: {resp.reason}"
            )

    def get_queue_size_before(self, prompt_id: str) -> int:
        """
        Retrieves the number of prompt in the queue before a prompt.

        Args:
            prompt_id (str): The ID of the prompt.

        Returns:
            int: The number of prompt in the queue before the prompt, 0 means the prompt is running.

        Raises:
            Exception: If the request fails with a non-200 status code.
            ValueError: If prompt_id is not in the queue.
        """
        resp = self.get_queue()
        for elem in resp["queue_running"]:
            if elem[1] == prompt_id:
                return 0

        result = 1
        for elem in resp["queue_pending"]:
            if elem[1] == prompt_id:
                return result
            result = result + 1
        raise ValueError("prompt_id is not in the queue")

    def get_history(self, prompt_id: str) -> dict:
        """
        Retrieves the execution history for a prompt.

        Args:
            prompt_id (str): The ID of the prompt.

        Returns:
            dict: The response JSON object.

        Raises:
            Exception: If the request fails with a non-200 status code.
        """
        url = urljoin(self.url, f"/history/{prompt_id}")
        _log.info(f"Getting history from {url}")
        resp = requests.get(url, auth=self.auth)
        if resp.status_code == 200:
            return resp.json()
        else:
            raise Exception(
                f"Request failed with status code {resp.status_code}: {resp.reason}"
            )

    def get_image(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        """
        Retrieves an image from the Comfy API server.

        Args:
            filename (str): The filename of the image.
            subfolder (str): The subfolder of the image.
            folder_type (str): The type of the folder.

        Returns:
            bytes: The content of the image.

        Raises:
            Exception: If the request fails with a non-200 status code.
        """
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        url = urljoin(self.url, f"/view?{urlencode(params)}")
        _log.info(f"Getting image from {url}")
        resp = requests.get(url, auth=self.auth)
        _log.debug(f"{resp.status_code}: {resp.reason}")
        if resp.status_code == 200:
            return resp.content
        else:
            raise Exception(
                f"Request failed with status code {resp.status_code}: {resp.reason}"
            )

    def upload_image(
        self, filename: str, subfolder: str = "default_upload_folder"
    ) -> dict:
        """
        Uploads an image to the Comfy API server.

        Args:
            filename (str): The filename of the image.
            subfolder (str): The subfolder to upload the image to. Defaults to "default_upload_folder".

        Returns:
            dict: The response JSON object.

        Raises:
            Exception: If the request fails with a non-200 status code.
        """
        url = urljoin(self.url, "/upload/image")
        serv_file = os.path.basename(filename)
        data = {"subfolder": subfolder}
        files = {"image": (serv_file, open(filename, "rb"))}
        _log.info(f"Posting {filename} to {url} with data {data}")
        resp = requests.post(url, files=files, data=data, auth=self.auth)
        _log.debug(f"{resp.status_code}: {resp.reason}, {resp.text}")
        if resp.status_code == 200:
            return resp.json()
        else:
            raise Exception(
                f"Request failed with status code {resp.status_code}: {resp.reason}"
            )

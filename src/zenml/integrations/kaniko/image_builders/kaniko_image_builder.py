#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""Kaniko image builder implementation."""

import io
import json
import random
import subprocess
import tarfile
import tempfile
from typing import TYPE_CHECKING, Any, BinaryIO, Dict, Optional, Type, cast

from zenml.config.base_settings import BaseSettings
from zenml.enums import StackComponentType
from zenml.image_builders import BaseImageBuilder
from zenml.integrations.kaniko.flavors import (
    KanikoImageBuilderConfig,
    KanikoImageBuilderSettings,
)
from zenml.stack import StackValidator
from zenml.logger import get_logger

if TYPE_CHECKING:
    from zenml.container_registries import BaseContainerRegistry
import shutil

logger = get_logger(__name__)

class KanikoImageBuilder(BaseImageBuilder):
    """Kaniko image builder implementation."""

    @property
    def config(self) -> KanikoImageBuilderConfig:
        """The stack component configuration.

        Returns:
            The configuration.
        """
        return cast(KanikoImageBuilderConfig, self._config)

    @property
    def settings_class(self) -> Optional[Type["BaseSettings"]]:
        """Settings class for the Kaniko image builder.

        Returns:
            The settings class.
        """
        return KanikoImageBuilderSettings

    @property
    def validator(self) -> Optional[StackValidator]:
        """Validates that the stack contains a container registry.

        Returns:
            Stack validator.
        """
        return StackValidator(
            required_components={StackComponentType.CONTAINER_REGISTRY},
        )

    def build_and_push(
        self,
        image_name: str,
        build_context_root: str,
        container_registry: "BaseContainerRegistry",
    ) -> str:
        """Builds and pushes a Docker image.

        Args:
            image_name: Name of the image to build and push.
            build_context_root: The root of the Docker build context.
            container_registry: The container registry to push to.

        Returns:
            The Docker image repo digest.
        """
        self._check_prerequisites()

        pod_name = self._generate_pod_name()
        spec_overrides = self._generate_spec_overrides(
            pod_name=pod_name, image_name=image_name
        )

        with tempfile.TemporaryFile(mode="w+b") as f:
            self._prepare_build_context(root=build_context_root, output_file=f)
            f.seek(0)
            self._run_kaniko_build(
                pod_name=pod_name,
                spec_overrides=spec_overrides,
                build_context=f,
            )

        image_name_with_sha = self._read_pod_output(pod_name=pod_name)
        self._verify_image_name(
            image_name_with_tag=image_name,
            image_name_with_sha=image_name_with_sha,
        )
        self._delete_pod(pod_name=pod_name)
        return image_name_with_sha

    def _prepare_build_context(self, root: str, output_file: BinaryIO) -> None:
        """Prepares the build context.

        Args:
            root: Root directory of the build context.
            output_file: The output file that should contain the build context
                archive.
        """
        tar = tarfile.open(mode="w:gz", fileobj=output_file)

        info = tarfile.TarInfo("Dockerfile")
        dockerfile_contents = "FROM alpine\nRUN echo 'Hello world'"
        contents_encoded = dockerfile_contents.encode("utf-8")
        info.size = len(contents_encoded)
        tar.addfile(info, io.BytesIO(contents_encoded))
        tar.close()

    def _generate_spec_overrides(
        self, pod_name: str, image_name: str
    ) -> Dict[str, Any]:
        """Generates Kubernetes spec overrides for the Kaniko build Pod.

        Args:
            pod_name: Name of the pod.
            image_name: Name of the image that should be built.

        Returns:
            Dictionary of spec override values.
        """
        args = [
            "--dockerfile=Dockerfile",
            "--context=tar://stdin",
            f"--destination={image_name}",
            # Use the image name with repo digest as the Pod termination
            # message. We use this later to read the image name using kubectl.
            "--image-name-with-digest-file=/dev/termination-log",
        ] + self.config.executor_args

        return {
            "apiVersion": "v1",
            "spec": {
                "containers": [
                    {
                        "name": pod_name,
                        "image": self.config.executor_image,
                        "stdin": True,
                        "stdinOnce": True,
                        "args": args,
                    }
                ]
            },
        }

    def _run_kaniko_build(
        self,
        pod_name: str,
        spec_overrides: Dict[str, Any],
        build_context: BinaryIO,
    ) -> None:
        """Runs the Kaniko build in Kubernetes.

        Args:
            pod_name: Name of the Pod that should be created to run the build.
            spec_overrides: Pod spec override values.
            build_context: Build context archive.

        Raises:
            RuntimeError: If the process running the Kaniko build failed.
        """
        command = [
            "kubectl",
            "--context",
            self.config.kubernetes_context,
            "--namespace",
            self.config.kubernetes_namespace,
            "run",
            pod_name,
            "--stdin",
            "true",
            "--restart",
            "Never",
            "--image",
            self.config.executor_image,
            "--overrides",
            json.dumps(spec_overrides),
        ]
        logger.debug("Running Kaniko build with command: %s", command)
        with subprocess.Popen(command, stdin=subprocess.PIPE) as p:
            with p.stdin:
                while True:
                    data = build_context.read(1024)
                    if not data:
                        break

                    p.stdin.write(data)
            try:
                return_code = p.wait()
            except:
                p.kill()
                raise

        if return_code:
            raise RuntimeError(
                "The process that runs the Kaniko build Pod failed. Check the "
                "log messages above for more information."
            )

    @staticmethod
    def _generate_pod_name() -> str:
        """Generates a random name for the Pod that runs the Kaniko build.

        Returns:
            The Pod name.
        """
        return f"kaniko-build-{random.Random().getrandbits(32):08x}"

    def _read_pod_output(self, pod_name: str) -> str:
        """Reads the Pod output message.

        Args:
            pod_name: Name of the Pod of which to read the output message.

        Returns:
            The Pod output message.
        """
        command = [
            "kubectl",
            "--context",
            self.config.kubernetes_context,
            "--namespace",
            self.config.kubernetes_namespace,
            "get",
            "pod",
            pod_name,
            "-o",
            'jsonpath="{.status.containerStatuses[0].state.terminated.message}"',
        ]
        output = subprocess.check_output(command).decode()
        output = output.strip('"')
        return output

    def _delete_pod(self, pod_name: str) -> None:
        """Deletes a Pod.

        Args:
            pod_name: Name of the Pod to delete.
        """
        command = [
            "kubectl",
            "--context",
            self.config.kubernetes_context,
            "--namespace",
            self.config.kubernetes_namespace,
            "delete",
            "pod",
            pod_name,
        ]
        subprocess.check_call(command)
        logger.debug("Deleted Pod %s.", pod_name)

    @staticmethod
    def _check_prerequisites() -> None:
        """Checks that all prerequisites are installed.

        Raises:
            RuntimeError: If any of the prerequisites are not installed.
        """
        if not shutil.which("kubectl"):
            raise RuntimeError(
                "`kubectl` is required to run the Kaniko image builder."
            )

    @staticmethod
    def _verify_image_name(
        image_name_with_tag: str, image_name_with_sha: str
    ) -> None:
        """Verifies the name/sha of the pushed image.

        Args:
            image_name_with_tag: The image name with a tag but without a unique
                sha.
            image_name_with_sha: The image name with a unique sha value
                appended.

        Raises:
            RuntimeError: If the image names don't point to the same Docker
                repository.
        """
        image_name_without_tag, _ = image_name_with_tag.rsplit(":", 1)
        if not image_name_with_sha.startswith(image_name_without_tag):
            raise RuntimeError(
                f"The Kaniko Pod output {image_name_with_sha} is not a valid "
                f"image name in the repository {image_name_without_tag}."
            )

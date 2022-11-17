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
"""Client implementation."""
import os
from abc import ABCMeta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Type,
    TypeVar,
    Union,
    cast,
)
from uuid import UUID

from zenml.config.global_config import GlobalConfiguration
from zenml.constants import (
    ENV_ZENML_ENABLE_REPO_INIT_WARNINGS,
    ENV_ZENML_REPOSITORY_PATH,
    REPOSITORY_DIRECTORY_NAME,
    handle_bool_env_var,
)
from zenml.enums import PermissionType, StackComponentType, StoreType
from zenml.exceptions import (
    AlreadyExistsException,
    IllegalOperationError,
    InitializationException,
    ValidationError,
)
from zenml.io import fileio
from zenml.logger import get_logger
from zenml.models import (
    ArtifactRequestModel,
    ComponentRequestModel,
    ComponentUpdateModel,
    FlavorRequestModel,
    PipelineRequestModel,
    PipelineResponseModel,
    PipelineRunRequestModel,
    PipelineRunResponseModel,
    ProjectRequestModel,
    ProjectResponseModel,
    ProjectUpdateModel,
    RoleAssignmentRequestModel,
    RoleAssignmentResponseModel,
    RoleRequestModel,
    RoleResponseModel,
    RoleUpdateModel,
    StackRequestModel,
    StackResponseModel,
    StackUpdateModel,
    StepRunRequestModel,
    TeamRequestModel,
    TeamResponseModel,
    UserRequestModel,
    UserResponseModel,
    UserUpdateModel,
)
from zenml.models.base_models import BaseResponseModel
from zenml.models.team_models import TeamUpdateModel
from zenml.utils import io_utils
from zenml.utils.analytics_utils import AnalyticsEvent, track
from zenml.utils.filesync_model import FileSyncModel

if TYPE_CHECKING:
    from zenml.config.pipeline_configurations import PipelineSpec
    from zenml.models import ComponentResponseModel, FlavorResponseModel
    from zenml.stack import Stack, StackComponentConfig
    from zenml.zen_stores.base_zen_store import BaseZenStore

logger = get_logger(__name__)
AnyResponseModel = TypeVar("AnyResponseModel", bound=BaseResponseModel)


class ClientConfiguration(FileSyncModel):
    """Pydantic object used for serializing client configuration options."""

    _active_project: Optional["ProjectResponseModel"] = None
    active_project_id: Optional[UUID]
    active_stack_id: Optional[UUID]

    @property
    def active_project(self):
        return self._active_project

    def set_active_project(self, project: "ProjectResponseModel") -> None:
        """Set the project for the local client.

        Args:
            project: The project to set active.
        """
        self._active_project = project
        self.active_project_id = project.id

    def set_active_stack(self, stack: "StackResponseModel") -> None:
        """Set the stack for the local client.

        Args:
            stack: The stack to set active.
        """
        self.active_stack_id = stack.id

    class Config:
        """Pydantic configuration class."""

        # Validate attributes when assigning them. We need to set this in order
        # to have a mix of mutable and immutable attributes
        validate_assignment = True
        # Allow extra attributes from configs of previous ZenML versions to
        # permit downgrading
        extra = "allow"
        # all attributes with leading underscore are private and therefore
        # are mutable and not included in serialization
        underscore_attrs_are_private = True


class ClientMetaClass(ABCMeta):
    """Client singleton metaclass.

    This metaclass is used to enforce a singleton instance of the Client
    class with the following additional properties:

    * the singleton Client instance is created on first access to reflect
    the global configuration and local client configuration.
    * the Client shouldn't be accessed from within pipeline steps (a warning
    is logged if this is attempted).
    """

    def __init__(cls, *args: Any, **kwargs: Any) -> None:
        """Initialize the Client class.

        Args:
            *args: Positional arguments.
            **kwargs: Keyword arguments.
        """
        super().__init__(*args, **kwargs)
        cls._global_client: Optional["Client"] = None

    def __call__(cls, *args: Any, **kwargs: Any) -> "Client":
        """Create or return the global Client instance.

        If the Client constructor is called with custom arguments,
        the singleton functionality of the metaclass is bypassed: a new
        Client instance is created and returned immediately and without
        saving it as the global Client singleton.

        Args:
            *args: Positional arguments.
            **kwargs: Keyword arguments.

        Returns:
            Client: The global Client instance.
        """
        if args or kwargs:
            return cast("Client", super().__call__(*args, **kwargs))

        if not cls._global_client:
            cls._global_client = cast(
                "Client", super().__call__(*args, **kwargs)
            )

        return cls._global_client


class Client(metaclass=ClientMetaClass):
    """ZenML client class.

    The ZenML client manages configuration options for ZenML stacks as well
    as their components.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
    ) -> None:
        """Initializes the global client instance.

        Client is a singleton class: only one instance can exist. Calling
        this constructor multiple times will always yield the same instance (see
        the exception below).

        The `root` argument is only meant for internal use and testing purposes.
        User code must never pass them to the constructor.
        When a custom `root` value is passed, an anonymous Client instance
        is created and returned independently of the Client singleton and
        that will have no effect as far as the rest of the ZenML core code is
        concerned.

        Instead of creating a new Client instance to reflect a different
        repository root, to change the active root in the global Client,
        call `Client().activate_root(<new-root>)`.

        Args:
            root: (internal use) custom root directory for the client. If
                no path is given, the repository root is determined using the
                environment variable `ZENML_REPOSITORY_PATH` (if set) and by
                recursively searching in the parent directories of the
                current working directory. Only used to initialize new
                clients internally.
        """
        self._root: Optional[Path] = None
        self._config: Optional[ClientConfiguration] = None

        self._set_active_root(root)

    @classmethod
    def get_instance(cls) -> Optional["Client"]:
        """Return the Client singleton instance.

        Returns:
            The Client singleton instance or None, if the Client hasn't
            been initialized yet.
        """
        return cls._global_client

    @classmethod
    def _reset_instance(cls, client: Optional["Client"] = None) -> None:
        """Reset the Client singleton instance.

        This method is only meant for internal use and testing purposes.

        Args:
            client: The Client instance to set as the global singleton.
                If None, the global Client singleton is reset to an empty
                value.
        """
        cls._global_client = client

    def _set_active_root(self, root: Optional[Path] = None) -> None:
        """Set the supplied path as the repository root.

        If a client configuration is found at the given path or the
        path, it is loaded and used to initialize the client.
        If no client configuration is found, the global configuration is
        used instead to manage the active stack, project etc.

        Args:
            root: The path to set as the active repository root. If not set,
                the repository root is determined using the environment
                variable `ZENML_REPOSITORY_PATH` (if set) and by recursively
                searching in the parent directories of the current working
                directory.
        """
        enable_warnings = handle_bool_env_var(
            ENV_ZENML_ENABLE_REPO_INIT_WARNINGS, True
        )
        self._root = self.find_repository(root, enable_warnings=enable_warnings)

        if not self._root:
            if enable_warnings:
                logger.info("Running without an active repository root.")
        else:
            logger.debug("Using repository root %s.", self._root)
            self._config = self._load_config()

        # Sanitize the client configuration to reflect the current
        # settings
        self._sanitize_config()

    def _config_path(self) -> Optional[str]:
        """Path to the client configuration file.

        Returns:
            Path to the client configuration file or None if the client
            root has not been initialized yet.
        """
        if not self.config_directory:
            return None
        return str(self.config_directory / "config.yaml")

    def _sanitize_config(self) -> None:
        """Sanitize and save the client configuration.

        This method is called to ensure that the client configuration
        doesn't contain outdated information, such as an active stack or
        project that no longer exists.
        """
        if not self._config:
            return

        active_project, active_stack = self.zen_store.validate_active_config(
            self._config.active_project_id,
            self._config.active_stack_id,
            config_name="repo",
        )
        self._config.set_active_stack(active_stack)
        self._config.set_active_project(active_project)

    def _load_config(self) -> Optional[ClientConfiguration]:
        """Loads the client configuration from disk.

        This happens if the client has an active root and the configuration
        file exists. If the configuration file doesn't exist, an empty
        configuration is returned.

        Returns:
            Loaded client configuration or None if the client does not
            have an active root.
        """
        config_path = self._config_path()
        if not config_path:
            return None

        # load the client configuration file if it exists, otherwise use
        # an empty configuration as default
        if fileio.exists(config_path):
            logger.debug(f"Loading client configuration from {config_path}.")
        else:
            logger.debug(
                "No client configuration file found, creating default "
                "configuration."
            )

        return ClientConfiguration(config_path)

    @staticmethod
    @track(event=AnalyticsEvent.INITIALIZE_REPO)
    def initialize(
        root: Optional[Path] = None,
    ) -> None:
        """Initializes a new ZenML repository at the given path.

        Args:
            root: The root directory where the repository should be created.
                If None, the current working directory is used.

        Raises:
            InitializationException: If the root directory already contains a
                ZenML repository.
        """
        root = root or Path.cwd()
        logger.debug("Initializing new repository at path %s.", root)
        if Client.is_repository_directory(root):
            raise InitializationException(
                f"Found existing ZenML repository at path '{root}'."
            )

        config_directory = str(root / REPOSITORY_DIRECTORY_NAME)
        io_utils.create_dir_recursive_if_not_exists(config_directory)
        # Initialize the repository configuration at the custom path
        Client(root=root)

    @property
    def uses_local_configuration(self) -> bool:
        """Check if the client is using a local configuration.

        Returns:
            True if the client is using a local configuration,
            False otherwise.
        """
        return self._config is not None

    @staticmethod
    def is_repository_directory(path: Path) -> bool:
        """Checks whether a ZenML client exists at the given path.

        Args:
            path: The path to check.

        Returns:
            True if a ZenML client exists at the given path,
            False otherwise.
        """
        config_dir = path / REPOSITORY_DIRECTORY_NAME
        return fileio.isdir(str(config_dir))

    @staticmethod
    def find_repository(
        path: Optional[Path] = None, enable_warnings: bool = False
    ) -> Optional[Path]:
        """Search for a ZenML repository directory.

        Args:
            path: Optional path to look for the repository. If no path is
                given, this function tries to find the repository using the
                environment variable `ZENML_REPOSITORY_PATH` (if set) and
                recursively searching in the parent directories of the current
                working directory.
            enable_warnings: If `True`, warnings are printed if the repository
                root cannot be found.

        Returns:
            Absolute path to a ZenML repository directory or None if no
            repository directory was found.
        """
        if not path:
            # try to get path from the environment variable
            env_var_path = os.getenv(ENV_ZENML_REPOSITORY_PATH)
            if env_var_path:
                path = Path(env_var_path)

        if path:
            # explicit path via parameter or environment variable, don't search
            # parent directories
            search_parent_directories = False
            warning_message = (
                f"Unable to find ZenML repository at path '{path}'. Make sure "
                f"to create a ZenML repository by calling `zenml init` when "
                f"specifying an explicit repository path in code or via the "
                f"environment variable '{ENV_ZENML_REPOSITORY_PATH}'."
            )
        else:
            # try to find the repository in the parent directories of the
            # current working directory
            path = Path.cwd()
            search_parent_directories = True
            warning_message = (
                f"Unable to find ZenML repository in your current working "
                f"directory ({path}) or any parent directories. If you "
                f"want to use an existing repository which is in a different "
                f"location, set the environment variable "
                f"'{ENV_ZENML_REPOSITORY_PATH}'. If you want to create a new "
                f"repository, run `zenml init`."
            )

        def _find_repository_helper(path_: Path) -> Optional[Path]:
            """Recursively search parent directories for a ZenML repository.

            Args:
                path_: The path to search.

            Returns:
                Absolute path to a ZenML repository directory or None if no
                repository directory was found.
            """
            if Client.is_repository_directory(path_):
                return path_

            if not search_parent_directories or io_utils.is_root(str(path_)):
                return None

            return _find_repository_helper(path_.parent)

        repository_path = _find_repository_helper(path)

        if repository_path:
            return repository_path.resolve()
        if enable_warnings:
            logger.warning(warning_message)
        return None

    @property
    def zen_store(self) -> "BaseZenStore":
        """Shortcut to return the global zen store.

        Returns:
            The global zen store.
        """
        return GlobalConfiguration().zen_store

    @property
    def root(self) -> Optional[Path]:
        """The root directory of this client.

        Returns:
            The root directory of this client, or None, if the client
            has not been initialized.
        """
        return self._root

    @property
    def config_directory(self) -> Optional[Path]:
        """The configuration directory of this client.

        Returns:
            The configuration directory of this client, or None, if the
            client doesn't have an active root.
        """
        if not self.root:
            return None
        return self.root / REPOSITORY_DIRECTORY_NAME

    def activate_root(self, root: Optional[Path] = None) -> None:
        """Set the active repository root directory.

        Args:
            root: The path to set as the active repository root. If not set,
                the repository root is determined using the environment
                variable `ZENML_REPOSITORY_PATH` (if set) and by recursively
                searching in the parent directories of the current working
                directory.
        """
        self._set_active_root(root)

    @track(event=AnalyticsEvent.SET_PROJECT)
    def set_active_project(
        self, project_name_or_id: Union[str, UUID]
    ) -> "ProjectResponseModel":
        """Set the project for the local client.

        Args:
            project_name_or_id: The name or ID of the project to set active.

        Returns:
            The model of the active project.
        """
        project = self.zen_store.get_project(
            project_name_or_id=project_name_or_id
        )  # raises KeyError
        if self._config:
            self._config.set_active_project(project)
        else:
            # set the active project globally only if the client doesn't use
            # a local configuration
            GlobalConfiguration().set_active_project(project)
        return project

    # ---- #
    # USER #
    # ---- #

    @property
    def active_user(self) -> "UserResponseModel":
        """Get the user that is currently in use.

        Returns:
            The active user.
        """
        return self.zen_store.active_user

    def create_user(
        self,
        name: str,
        initial_role: Optional[str] = None,
        password: Optional[str] = None,
    ) -> UserResponseModel:
        user = UserRequestModel(name=name, password=password or None)
        if self.zen_store.type != StoreType.REST:
            user.active = password != ""
        else:
            user.active = True

        created_user = self.zen_store.create_user(user=user)

        if initial_role:
            self.create_role_assignment(
                role_name_or_id=initial_role,
                user_or_team_name_or_id=created_user.id,
                project_name_or_id=None,
                is_user=True,
            )

        return created_user

    def get_user(self, name_id_or_prefix: str) -> UserResponseModel:
        """Gets a user.

        Args:
            name_id_or_prefix: The name or ID of the user.

        Returns:
            The User
        """
        return self._get_entity_by_id_or_name_or_prefix(
            response_model=UserResponseModel,
            get_method=self.zen_store.get_user,
            list_method=self.zen_store.list_users,
            name_id_or_prefix=name_id_or_prefix,
        )

    def delete_user(self, user_name_or_id: str) -> None:
        """Delete a user.

        Args:
            user_name_or_id: The name or ID of the user to delete.

        Raises:
            IllegalOperationError: If the user to delete is the active user.
        """
        user = self.get_user(user_name_or_id)
        if self.zen_store.active_user_name == user.name:
            raise IllegalOperationError(
                "You cannot delete yourself. If you wish to delete your active "
                "user account, please contact your ZenML administrator."
            )
        self.zen_store.delete_user(user_name_or_id=user.name)

    def update_user(
        self,
        user_name_or_id: Union[str, UUID],
        updated_name: Optional[str] = None,
        updated_full_name: Optional[str] = None,
        updated_email: Optional[str] = None,
    ) -> UserResponseModel:
        user = self._get_entity_by_id_or_name_or_prefix(
            response_model=UserResponseModel,
            get_method=self.zen_store.get_user,
            list_method=self.zen_store.list_users,
            name_id_or_prefix=user_name_or_id,
        )

        user_update = UserUpdateModel()
        if updated_name:
            user_update.name = updated_name
        if updated_full_name:
            user_update.full_name = updated_full_name
        if updated_email:
            user_update.email = updated_email
        return self.zen_store.update_user(
            user_name_or_id=user.id, user_update=user_update
        )

    # ---- #
    # TEAM #
    # ---- #

    def get_team(self, name_id_or_prefix: str) -> TeamResponseModel:
        """Gets a team.

        Args:
            name_id_or_prefix: The name or ID of the team.

        Returns:
            The Team
        """
        return self._get_entity_by_id_or_name_or_prefix(
            response_model=TeamResponseModel,
            get_method=self.zen_store.get_team,
            list_method=self.zen_store.list_teams,
            name_id_or_prefix=name_id_or_prefix,
        )

    def list_teams(self, name: Optional[str] = None) -> List[TeamResponseModel]:
        """List all teams.

        Args:
            name: The name to filter by

        Returns:
            The Team
        """
        return self.zen_store.list_teams(name=name)

    def create_team(
        self, name: str, users: Optional[List[str]] = None
    ) -> TeamResponseModel:
        """Create a team.

        Args:
            name: Name of the new team
            users: Users of the new team
        """
        user_list = []
        if users:
            for user_name_or_id in users:
                user_list.append(
                    self.get_user(name_id_or_prefix=user_name_or_id).id
                )

        team = TeamRequestModel(name=name, users=user_list)

        return self.zen_store.create_team(team=team)

    def delete_team(self, team_name_or_id: str) -> None:
        """Delete a team.

        Args:
            team_name_or_id: The name or ID of the team to delete.
        """
        team = self.get_team(team_name_or_id)
        self.zen_store.delete_team(team_name_or_id=team.name)

    def update_team(
        self,
        team_name_or_id: str,
        new_name: Optional[str] = None,
        remove_users: Optional[List[str]] = None,
        add_users: Optional[List[str]] = None,
    ) -> TeamResponseModel:
        team = self._get_entity_by_id_or_name_or_prefix(
            response_model=TeamResponseModel,
            get_method=self.zen_store.get_team,
            list_method=self.zen_store.list_teams,
            name_id_or_prefix=team_name_or_id,
        )

        team_update = TeamUpdateModel()
        if new_name:
            team_update.name = new_name

        team_users: Optional[List[UUID]] = None

        union_add_rm = set(remove_users) & set(add_users)
        if union_add_rm:
            raise RuntimeError(
                f"The `remove_user` and `add_user` "
                f"options both contain the same value(s): "
                f"`{union_add_rm}`. Please rerun command and make sure "
                f"that the same user does not show up for "
                f"`remove_user` and `add_user`."
            )
        # Only if permissions are being added or removed will they need to be
        #  set for the update model
        if remove_users or add_users:
            team_users = [u.id for u in team.users]
        if remove_users:
            for rm_p in remove_users:
                user = self.get_user(rm_p)
                try:
                    team_users.remove(user.id)
                except KeyError:
                    logger.warning(
                        f"Role {remove_users} was already not "
                        f"part of the '{team.name}' Team."
                    )
        if add_users:
            for add_u in add_users:
                team_users.append(self.get_user(add_u).id)

        if team_users:
            team_update.users = team_users

        return self.zen_store.update_team(
            team_id=team.id, team_update=team_update
        )

    # ----- #
    # ROLES #
    # ----- #

    def get_role(self, name_id_or_prefix: str) -> RoleResponseModel:
        """Gets a role.

        Args:
            name_id_or_prefix: The name or ID of the role.

        Returns:
            The User
        """
        return self._get_entity_by_id_or_name_or_prefix(
            response_model=RoleResponseModel,
            get_method=self.zen_store.get_role,
            list_method=self.zen_store.list_roles,
            name_id_or_prefix=name_id_or_prefix,
        )

    def list_roles(self, name: Optional[str] = None) -> List[RoleResponseModel]:
        """Gets a user.

        Args:
            name: The name of the roles.

        Returns:
            The User
        """
        return self.zen_store.list_roles(name=name)

    def create_role(
        self, name: str, permissions_list: List[str]
    ) -> RoleResponseModel:
        """Gets a user.

        Args:
            name: The name for the new role.
            permissions_list: The permissions to attach to this role.

        Returns:
            The newly created role
        """
        permissions: Set[PermissionType] = set()
        for permission in permissions_list:
            if permission in PermissionType.values():
                permissions.add(PermissionType(permission))

        new_role = RoleRequestModel(name=name, permissions=permissions)
        return self.zen_store.create_role(new_role)

    def update_role(
        self,
        name_id_or_prefix: str,
        new_name: Optional[str] = None,
        remove_permission: Optional[List[str]] = None,
        add_permission: Optional[List[str]] = None,
    ) -> RoleResponseModel:
        """Gets a user.

        Args:
            name_id_or_prefix: The name or ID of the user.
            new_name: The new name for the role
            remove_permission: Permissions to remove from this role
            add_permission: Permissions to add to this role

        Returns:
            The User
        """
        role = self._get_entity_by_id_or_name_or_prefix(
            response_model=RoleResponseModel,
            get_method=self.zen_store.get_role,
            list_method=self.zen_store.list_roles,
            name_id_or_prefix=name_id_or_prefix,
        )
        role_update = RoleUpdateModel()

        role_permissions = None

        union_add_rm = set(remove_permission) & set(add_permission)
        if union_add_rm:
            raise RuntimeError(
                f"The `remove_permission` and `add_permission` "
                f"options both contain the same value(s): "
                f"`{union_add_rm}`. Please rerun command and make sure "
                f"that the same role does not show up for "
                f"`remove_permission` and `add_permission`."
            )
        # Only if permissions are being added or removed will they need to be
        #  set for the update model
        if remove_permission or add_permission:
            role_permissions = role.permissions
        if remove_permission:
            for rm_p in remove_permission:
                if rm_p in PermissionType:
                    try:
                        role_permissions.remove(PermissionType(rm_p))
                    except KeyError:
                        logger.warning(
                            f"Role {remove_permission} was already not "
                            f"part of the {role} Role."
                        )
        if add_permission:
            for add_p in add_permission:
                if add_p in PermissionType.values():
                    # Set won't throw an error if the item was already in it
                    role_permissions.add(PermissionType(add_p))

        if role_permissions:
            role_update.permissions = set(role_permissions)
        if new_name:
            role_update.name = new_name

        return Client().zen_store.update_role(
            role_id=role.id, role_update=role_update
        )

    def delete_role(self, name_id_or_prefix: str) -> None:
        """Gets a user.

        Args:
            name_id_or_prefix: The name or ID of the user.
        """
        self.zen_store.delete_role(role_name_or_id=name_id_or_prefix)

    # ---------------- #
    # ROLE ASSIGNMENTS #
    # ---------------- #

    def get_role_assignment(
        self,
        role_name_or_id: str,
        user_or_team_name_or_id: str,
        is_user: bool,
        project_name_or_id: Optional[str] = None,
    ) -> RoleAssignmentResponseModel:
        """Get a role assignment.

        Args:
            role_name_or_id: Role to assign
            user_or_team_name_or_id: team to assign the role to
            is_user: Whether to interpret the user_or_team_name_or_id field as
                user (=True) or team (=False)
            project_name_or_id: project scope within which to assign the role
        """
        if is_user:
            role_assignments = self.zen_store.list_role_assignments(
                project_name_or_id=project_name_or_id,
                user_name_or_id=user_or_team_name_or_id,
                role_name_or_id=role_name_or_id,
            )
        else:
            role_assignments = self.zen_store.list_role_assignments(
                project_name_or_id=project_name_or_id,
                user_name_or_id=user_or_team_name_or_id,
                role_name_or_id=role_name_or_id,
            )
        # Implicit assumption is that maximally one such assignment can exists
        if role_assignments:
            return role_assignments[0]
        else:
            raise RuntimeError(
                "No such role assignment could be found for "
                f"user/team : {user_or_team_name_or_id} with "
                f"role : {role_name_or_id} within "
                f"project : {project_name_or_id}"
            )

    def create_role_assignment(
        self,
        role_name_or_id: Union[str, UUID],
        user_or_team_name_or_id: Union[str, UUID],
        is_user: bool,
        project_name_or_id: Optional[Union[str, UUID]] = None,
    ) -> RoleAssignmentResponseModel:
        """Create a role assignment.

        Args:
            role_name_or_id: Role to assign
            user_or_team_name_or_id: team to assign the role to
            is_user: Whether to interpret the user_or_team_name_or_id field as
                user (=True) or team (=False)
            project_name_or_id: project scope within which to assign the role

        """
        role = self._get_entity_by_id_or_name_or_prefix(
            response_model=RoleResponseModel,
            get_method=self.zen_store.get_role,
            list_method=self.zen_store.list_roles,
            name_id_or_prefix=role_name_or_id,
        )
        project = None
        if project_name_or_id:
            project = self._get_entity_by_id_or_name_or_prefix(
                response_model=ProjectResponseModel,
                get_method=self.zen_store.get_project,
                list_method=self.zen_store.list_projects,
                name_id_or_prefix=project_name_or_id,
            )
        if is_user:
            user = self._get_entity_by_id_or_name_or_prefix(
                response_model=UserResponseModel,
                get_method=self.zen_store.get_user,
                list_method=self.zen_store.list_users,
                name_id_or_prefix=user_or_team_name_or_id,
            )
            role_assignment = RoleAssignmentRequestModel(
                role=role.id,
                user=user.id,
                project=project,
                is_user=True,
            )
        else:
            team = self._get_entity_by_id_or_name_or_prefix(
                response_model=TeamResponseModel,
                get_method=self.zen_store.get_team,
                list_method=self.zen_store.list_teams,
                name_id_or_prefix=user_or_team_name_or_id,
            )
            role_assignment = RoleAssignmentRequestModel(
                role=role.id,
                team=team.id,
                project=project,
                is_user=False,
            )

        return self.zen_store.create_role_assignment(
            role_assignment=role_assignment
        )

    def delete_role_assignment(
        self,
        role_name_or_id: str,
        user_or_team_name_or_id: str,
        is_user: bool,
        project_name_or_id: Optional[str] = None,
    ) -> None:
        """Delete a role assignment.

        Args:
            role_name_or_id: Role to assign
            user_or_team_name_or_id: team to assign the role to
            is_user: Whether to interpret the user_or_team_name_or_id field as
                user (=True) or team (=False)
            project_name_or_id: project scope within which to assign the role
        """
        role_assignment = self.get_role_assignment(
            role_name_or_id=role_name_or_id,
            user_or_team_name_or_id=user_or_team_name_or_id,
            is_user=is_user,
            project_name_or_id=project_name_or_id,
        )
        self.zen_store.delete_role_assignment(role_assignment.id)

    def list_role_assignment(
        self,
        role_name_or_id: Optional[str] = None,
        user_name_or_id: Optional[str] = None,
        team_name_or_id: Optional[str] = None,
        project_name_or_id: Optional[str] = None,
    ) -> List[RoleAssignmentResponseModel]:
        return self.zen_store.list_role_assignments(
            project_name_or_id=project_name_or_id,
            role_name_or_id=role_name_or_id,
            user_name_or_id=user_name_or_id,
            team_name_or_id=team_name_or_id,
        )

    # ------- #
    # PROJECT #
    # ------- #

    @property
    def active_project(self) -> "ProjectResponseModel":
        """Get the currently active project of the local client.

        If no active project is configured locally for the client, the
        active project in the global configuration is used instead.

        Returns:
            The active project.

        Raises:
            RuntimeError: If the active project is not set.
        """
        project: Optional["ProjectResponseModel"] = None
        if self._config:
            project = self._config.active_project

        if not project:
            project = GlobalConfiguration().active_project

        if not project:
            raise RuntimeError(
                "No active project is configured. Run "
                "`zenml project set PROJECT_NAME` to set the active "
                "project."
            )

        from zenml.zen_stores.base_zen_store import DEFAULT_PROJECT_NAME

        if project.name != DEFAULT_PROJECT_NAME:
            logger.warning(
                f"You are running with a non-default project "
                f"'{project.name}'. Any stacks, components, "
                f"pipelines and pipeline runs produced in this "
                f"project will currently not be accessible through "
                f"the dashboard. However, this will be possible "
                f"in the near future."
            )
        return project

    def get_project(self, name_id_or_prefix: str) -> ProjectResponseModel:
        """Gets a project.

        Args:
            name_id_or_prefix: The name or ID of the project.

        Returns:
            The Project
        """
        return self._get_entity_by_id_or_name_or_prefix(
            response_model=ProjectResponseModel,
            get_method=self.zen_store.get_project,
            list_method=self.zen_store.list_projects,
            name_id_or_prefix=name_id_or_prefix,
        )

    def create_project(
        self, name: str, description: str
    ) -> "ProjectResponseModel":
        """Create a new project.

        Args:
            name: Name of the project
            description: Description of the project
        """
        return self.zen_store.create_project(
            ProjectRequestModel(name=name, description=description)
        )

    def update_project(
        self,
        name: str,
        new_name: Optional[str] = None,
        new_description: Optional[str] = None,
    ) -> "ProjectResponseModel":
        """Create a new project.

        Args:
            name: Name of the project
            new_name: Name of the project
            new_description: Description of the project
        """
        project = self._get_entity_by_id_or_name_or_prefix(
            response_model=ProjectResponseModel,
            get_method=self.zen_store.get_project,
            list_method=self.zen_store.list_projects,
            name_id_or_prefix=name,
        )
        project_update = ProjectUpdateModel()
        if new_name:
            project_update.name = new_name
        if new_description:
            project_update.description = new_description
        return self.zen_store.update_project(
            project_id=project.id,
            project_update=project_update,
        )

    def delete_project(self, project_name_or_id: str) -> None:
        """Delete a project.

        Args:
            project_name_or_id: The name or ID of the project to delete.

        Raises:
            IllegalOperationError: If the project to delete is the active
                project.
        """
        project = self.zen_store.get_project(project_name_or_id)
        if self.active_project.id == project.id:
            raise IllegalOperationError(
                f"Project '{project_name_or_id}' cannot be deleted since it is "
                "currently active. Please set another project as active first."
            )
        self.zen_store.delete_project(project_name_or_id=project_name_or_id)

    # ------ #
    # STACKS #
    # ------ #
    @property
    def active_stack_model(self) -> "StackResponseModel":
        """The model of the active stack for this client.

        If no active stack is configured locally for the client, the active
        stack in the global configuration is used instead.

        Returns:
            The model of the active stack for this client.

        Raises:
            RuntimeError: If the active stack is not set.
        """
        stack: Optional["StackResponseModel"] = None

        if self._config:
            stack = self.get_stack(self._config.active_stack_id)

        if not stack:
            stack = self.get_stack(GlobalConfiguration().active_stack_id)

        if not stack:
            raise RuntimeError(
                "No active stack is configured. Run "
                "`zenml stack set PROJECT_NAME` to set the active "
                "stack."
            )

        return stack

    @property
    def active_stack(self) -> "Stack":
        """The active stack for this client.

        Returns:
            The active stack for this client.
        """
        from zenml.stack.stack import Stack

        return Stack.from_model(self.active_stack_model)

    def get_stack(
        self, name_id_or_prefix: Optional[Union[UUID, str]] = None
    ) -> "StackResponseModel":
        """Get Stack.

        Args:
            name_id_or_prefix: ID of the pipeline.

        Raises:
            KeyError: If the name_id_or_prefix does not uniquely identify one
                stack
        """
        if name_id_or_prefix is not None:
            return self._get_entity_by_id_or_name_or_prefix(
                response_model=StackResponseModel,
                get_method=self.zen_store.get_stack,
                list_method=self.zen_store.list_stacks,
                name_id_or_prefix=name_id_or_prefix,
            )
        else:
            return self.active_stack_model

    def register_stack(
        self,
        name: str,
        components: Dict[StackComponentType, str],
        is_shared: bool = False,
    ) -> "StackResponseModel":
        """Registers a stack and its components.

        Args:
            name: The name of the stack to register.
            components: dictionary which maps component types to component names
            is_shared: boolean to decide whether the stack is shared

        Returns:
            The model of the registered stack.
        """

        stack_components = dict()

        for c_type, c_name in components.items():
            if c_name:
                stack_components[c_type] = [
                    self.get_stack_component(
                        name_id_or_prefix=c_name,
                        component_type=c_type,
                    ).id
                ]

        stack = StackRequestModel(
            name=name,
            components=stack_components,
            is_shared=is_shared,
            project=self.active_project.id,
            user=self.active_user.id,
        )

        self._validate_stack_configuration(stack=stack)

        return self.zen_store.create_stack(stack=stack)

    def update_stack(
        self,
        name_id_or_prefix: Union[str, UUID],
        name: Optional[str] = None,
        is_shared: Optional[bool] = None,
        description: Optional[str] = None,
        component_updates: Optional[
            Dict[StackComponentType, List[Optional[str]]]
        ] = None,
    ) -> "StackResponseModel":
        """Updates a stack and its components.

        Args:
            name_id_or_prefix: The name, id or the id prefix of the
                stack which is getting updated.
            name: the updated name of the stack
            is_shared: the updated shared status of the stack
            description: the updated description of the stack
            component_updates: dictionary which maps stack component types to
                updated list of names.
        """
        # First, get the stack
        stack = self.get_stack(name_id_or_prefix=name_id_or_prefix)

        # Create the update model
        update_model = StackUpdateModel(
            project=self.active_project.id,
            user=self.active_user.id,
        )

        if name:
            shared_status = is_shared or stack.is_shared

            existing_stacks = self.list_stacks(
                name=name, is_shared=shared_status
            )
            if existing_stacks:
                raise ValueError(
                    "There are already existing stacks with the name "
                    f"'{name}'."
                )

            update_model.name = name

        if is_shared:
            existing_stacks = self.list_stacks(name=name, is_shared=True)
            if existing_stacks:
                raise ValueError(
                    "There are already existing shared stacks with the name "
                    f"'{name}'."
                )

            for component_type, components in stack.components.items():
                for c in components:
                    self.update_stack_component(
                        name_id_or_prefix=c.id,
                        component_type=component_type,
                        is_shared=True,
                    )
            update_model.is_shared = is_shared

        if description:
            update_model.description = description

        # Get the current components
        if component_updates:
            components = {}
            for component_type, component_list in stack.components.items():
                if component_list is not None:
                    components[component_type] = [c.id for c in component_list]

            for component_type, component_list in component_updates.items():
                if component_list is not None:
                    components[component_type] = [
                        self.get_stack_component(
                            name_id_or_prefix=c,
                            component_type=component_type,
                        ).id
                        for c in component_list
                    ]

            update_model.components = components

        return self.zen_store.update_stack(
            stack_id=stack.id,
            stack_update=update_model,
        )

    def deregister_stack(self, name_id_or_prefix: Union[str, UUID]) -> None:
        """Deregisters a stack.

        Args:
            name_id_or_prefix: The name, id or prefix id of the stack
                to deregister.

        Raises:
            ValueError: If the stack is the currently active stack for this
                client.
        """
        stack = self.get_stack(name_id_or_prefix=name_id_or_prefix)

        if stack.id == self.active_stack_model.id:
            raise ValueError(
                f"Unable to deregister active stack '{stack.name}'. Make "
                f"sure to designate a new active stack before deleting this "
                f"one."
            )

        cfg = GlobalConfiguration()
        if stack.id == cfg.active_stack_id:
            raise ValueError(
                f"Unable to deregister '{stack.name}' as it is the active "
                f"stack within your global configuration. Make "
                f"sure to designate a new active stack before deleting this "
                f"one."
            )

        self.zen_store.delete_stack(stack_id=stack.id)
        logger.info("Deregistered stack with name '%s'.", stack.name)

    def list_stacks(
        self,
        project_name_or_id: Optional[Union[str, UUID]] = None,
        user_name_or_id: Optional[Union[str, UUID]] = None,
        component_id: Optional[UUID] = None,
        name: Optional[str] = None,
        is_shared: Optional[bool] = None,
    ) -> List["StackResponseModel"]:
        """"""
        return self.zen_store.list_stacks(
            project_name_or_id=project_name_or_id or self.active_project.id,
            user_name_or_id=user_name_or_id or self.active_user.id,
            component_id=component_id,
            name=name,
            is_shared=is_shared,
        )

    @track(event=AnalyticsEvent.SET_STACK)
    def activate_stack(self, stack_name_id_or_prefix: Union[str, UUID]) -> None:
        """Sets the stack as active.

        Args:
            stack_name_id_or_prefix: Model of the stack to activate.

        Raises:
            KeyError: If the stack is not registered.
        """
        # Make sure the stack is registered
        try:
            stack = self.get_stack(name_id_or_prefix=stack_name_id_or_prefix)

        except KeyError:
            raise KeyError(
                f"Stack '{stack_name_id_or_prefix}' cannot be activated since "
                f"it is not registered yet. Please register it first."
            )

        if self._config:
            self._config.set_active_stack(stack=stack)

        else:
            # set the active stack globally only if the client doesn't use
            # a local configuration
            GlobalConfiguration().set_active_stack(stack=stack)

    def _validate_stack_configuration(self, stack: "StackRequestModel") -> None:
        """Validates the configuration of a stack.

        Args:
            stack: The stack to validate.

        Raises:
            KeyError: If the stack references missing components.
            ValidationError: If the stack configuration is invalid.
        """
        local_components: List[str] = []
        remote_components: List[str] = []
        for component_type, component_ids in stack.components.items():
            for component_id in component_ids:
                try:
                    component = self.get_stack_component(
                        name_id_or_prefix=component_id,
                        component_type=component_type,
                    )
                except KeyError:
                    raise KeyError(
                        f"Cannot register stack '{stack.name}' since it has an "
                        f"unregistered {component_type} with id "
                        f"'{component_id}'."
                    )
            # Get the flavor model
            flavor_model = self.get_flavor_by_name_and_type(
                name=component.flavor, component_type=component.type
            )

            # Create and validate the configuration
            from zenml.stack import Flavor

            flavor = Flavor.from_model(flavor_model)
            configuration = flavor.config_class(**component.configuration)
            if configuration.is_local:
                local_components.append(
                    f"{component.type.value}: {component.name}"
                )
            elif configuration.is_remote:
                remote_components.append(
                    f"{component.type.value}: {component.name}"
                )

        if local_components and remote_components:
            logger.warning(
                f"You are configuring a stack that is composed of components "
                f"that are relying on local resources "
                f"({', '.join(local_components)}) as well as "
                f"components that are running remotely "
                f"({', '.join(remote_components)}). This is not recommended as "
                f"it can lead to unexpected behavior, especially if the remote "
                f"components need to access the local resources. Please make "
                f"sure that your stack is configured correctly, or try to use "
                f"component flavors or configurations that do not require "
                f"local resources."
            )

        if not stack.is_valid:
            raise ValidationError(
                "Stack configuration is invalid. A valid"
                "stack must contain an Artifact Store and "
                "an Orchestrator."
            )

    # .------------.
    # | COMPONENTS |
    # '------------'
    def get_stack_component(
        self,
        component_type: StackComponentType,
        name_id_or_prefix: Optional[Union[str, UUID]] = None,
    ) -> "ComponentResponseModel":
        """Fetches a registered stack component.

        If the name_id_or_prefix is provided, it will try to fetch the component
        with the corresponding identifier. If not, it will try to fetch the
        active component of the given type.

        Args:
            component_type: The type of the component to fetch
            name_id_or_prefix: The id of the component to fetch.

        Returns:
            The registered stack component.
        """
        if name_id_or_prefix is not None:
            return self._get_component_by_id_or_name_or_prefix(
                name_id_or_prefix=name_id_or_prefix,
                component_type=component_type,
            )
        else:
            components = self.active_stack_model.components.get(
                component_type, None
            )
            if components is None:
                raise KeyError(
                    "No name_id_or_prefix provided and there is no active "
                    f"{component_type} in the current active stack."
                )

            return components[0]

    def list_stack_components(
        self,
        project_name_or_id: Optional[Union[str, UUID]] = None,
        user_name_or_id: Optional[Union[str, UUID]] = None,
        component_type: Optional[str] = None,
        flavor_name: Optional[str] = None,
        name: Optional[str] = None,
        is_shared: Optional[bool] = None,
    ) -> List["ComponentResponseModel"]:
        """"""
        return self.zen_store.list_stack_components(
            project_name_or_id=project_name_or_id or self.active_project.id,
            user_name_or_id=user_name_or_id or self.active_user.id,
            type=component_type,
            flavor_name=flavor_name,
            name=name,
            is_shared=is_shared,
        )

    def register_stack_component(
        self,
        name: str,
        flavor: str,
        component_type: StackComponentType,
        configuration: Dict[str, str],
        is_shared: bool = False,
    ) -> "ComponentResponseModel":
        """Registers a stack component.

        Args:
            name:
            flavor:
            component_type:
            configuration:
            is_shared:

        Returns:
            The model of the registered component.
        """
        # Get the flavor model
        flavor_model = self.get_flavor_by_name_and_type(
            name=flavor,
            component_type=component_type,
        )

        # Create and validate the configuration
        from zenml.stack import Flavor

        flavor_class = Flavor.from_model(flavor_model)
        configuration = flavor_class.config_class(**configuration)

        self._validate_stack_component_configuration(
            component_type, configuration=configuration
        )

        create_component_model = ComponentRequestModel(
            name=name,
            type=component_type,
            flavor=flavor,
            configuration=configuration,
            is_shared=is_shared,
            user=self.active_user.id,
            project=self.active_project.id,
        )

        # Register the new model
        return self.zen_store.create_stack_component(
            component=create_component_model
        )

    def update_stack_component(
        self,
        name_id_or_prefix: Union[str, UUID],
        component_type: StackComponentType,
        name: Optional[str] = None,
        configuration: Optional[Dict[str, str]] = None,
        is_shared: Optional[bool] = None,
    ) -> "ComponentResponseModel":
        """Updates a stack component.

        Args:
            name_id_or_prefix:
            component_type:
            name:
            configuration:
            is_shared:

        Returns:
            The updated component.
        """
        # Get the existing component model
        component = self.get_stack_component(
            name_id_or_prefix=name_id_or_prefix,
            component_type=component_type,
        )

        update_model = ComponentUpdateModel(
            project=self.active_project.id,
            user=self.active_user.id,
        )

        if name is not None:
            shared_status = is_shared or component.is_shared

            existing_components = self.list_stack_components(
                name=name,
                is_shared=shared_status,
                component_type=component_type,
            )
            if existing_components:
                raise ValueError(
                    f"There are already existing "
                    f"{'shared' if shared_status else 'unshared'} components "
                    f"with the name '{name}'."
                )
            update_model.name = name

        if is_shared is not None:
            existing_components = self.list_stack_components(
                name=name, is_shared=True, component_type=component_type
            )
            if existing_components:
                raise ValueError(
                    f"There are already existing shared components with "
                    f"the name '{name}'"
                )
            update_model.is_shared = is_shared

        if configuration is not None:
            existing_configuration = component.configuration
            existing_configuration.update(configuration)

            existing_configuration = {
                k: v for k, v in existing_configuration.items() if v is not None
            }

            flavor_model = self.get_flavor_by_name_and_type(
                name=component.flavor,
                component_type=component.type,
            )

            from zenml.stack import Flavor

            flavor = Flavor.from_model(flavor_model)
            configuration_obj = flavor.config_class(**existing_configuration)

            self._validate_stack_component_configuration(
                component.type, configuration=configuration_obj
            )
            update_model.configuration = existing_configuration

        # Send the updated component to the ZenStore
        return self.zen_store.update_stack_component(
            component_id=component.id,
            component_update=update_model,
        )

    def deregister_stack_component(
        self,
        name_id_or_prefix: Union[str, UUID],
        component_type: StackComponentType,
    ) -> None:
        """Deletes a registered stack component.

        Args:
            name_id_or_prefix: The model of the component to delete.
            component_type: The type of the component to delete.
        """
        component = self.get_stack_component(
            name_id_or_prefix=name_id_or_prefix,
            component_type=component_type,
        )

        self.zen_store.delete_stack_component(component_id=component.id)
        logger.info(
            "Deregistered stack component (type: %s) with name '%s'.",
            component.type,
            component.name,
        )

    def _validate_stack_component_configuration(
        self,
        component_type: "StackComponentType",
        configuration: "StackComponentConfig",
    ) -> None:
        """Validates the configuration of a stack component.

        Args:
            component_type: The type of the component.
            configuration: The component configuration to validate.
        """
        from zenml.enums import StackComponentType, StoreType

        if configuration.is_remote and self.zen_store.is_local_store():
            if self.zen_store.type == StoreType.REST:
                logger.warning(
                    "You are configuring a stack component that is running "
                    "remotely while using a local database. The component "
                    "may not be able to reach the local database and will "
                    "therefore not be functional. Please consider deploying "
                    "and/or using a remote ZenML server instead."
                )
            else:
                logger.warning(
                    "You are configuring a stack component that is running "
                    "remotely while using a local ZenML server. The component "
                    "may not be able to reach the local ZenML server and will "
                    "therefore not be functional. Please consider deploying "
                    "and/or using a remote ZenML server instead."
                )
        elif configuration.is_local and not self.zen_store.is_local_store():
            logger.warning(
                "You are configuring a stack component that is using "
                "local resources while connected to a remote ZenML server. The "
                "stack component may not be usable from other hosts or by "
                "other users. You should consider using a non-local stack "
                "component alternative instead."
            )
            if component_type in [
                StackComponentType.ORCHESTRATOR,
                StackComponentType.STEP_OPERATOR,
            ]:
                logger.warning(
                    "You are configuring a stack component that is running "
                    "pipeline code on your local host while connected to a "
                    "remote ZenML server. This will significantly affect the "
                    "performance of your pipelines. You will likely encounter "
                    "long running times caused by network latency. You should "
                    "consider using a non-local stack component alternative "
                    "instead."
                )

    # .---------.
    # | FLAVORS |
    # '---------'

    def create_flavor(
        self,
        source: str,
        component_type: StackComponentType,
    ) -> "FlavorResponseModel":
        """Creates a new flavor.

        Args:
            source: The flavor to create.
            component_type: The type of the flavor.

        Returns:
            The created flavor (in model form).
        """
        from zenml.utils.source_utils import validate_flavor_source

        flavor = validate_flavor_source(
            source=source,
            component_type=component_type,
        )()

        create_flavor_request = FlavorRequestModel(
            source=source,
            type=flavor.type,
            name=flavor.name,
            config_schema=flavor.config_schema,
        )

        return self.zen_store.create_flavor(flavor=create_flavor_request)

    def get_flavor(self, name_id_or_prefix: str) -> "FlavorResponseModel":
        """Get a stack component flavor.

        Args:
            name_id_or_prefix: The name, ID or prefix to the id of the flavor
                to get.

        Returns:
            The stack component flavor.

        Raises:
            KeyError: if the stack component flavor doesn't exist.
        """
        return self._get_entity_by_id_or_name_or_prefix(
            response_model=FlavorResponseModel,
            get_method=self.zen_store.get_flavor,
            list_method=self.zen_store.list_flavors,
            name_id_or_prefix=name_id_or_prefix,
        )

    def delete_flavor(self, name_id_or_prefix: str) -> None:
        """Deletes a flavor.

        Args:
            name_id_or_prefix: The name, id or prefix of the id for the
                flavor to delete.
        """
        flavor = self.get_flavor(name_id_or_prefix)
        self.zen_store.delete_flavor(flavor_id=flavor.id)

        logger.info(f"Deleted flavor '{flavor.name}' of type '{flavor.type}'.")

    def list_flavors(
        self,
    ) -> List["FlavorResponseModel"]:
        """Fetches all the flavor models.

        Returns:
            A list of all the flavor models.
        """
        from zenml.stack.flavor_registry import flavor_registry

        zenml_flavors = flavor_registry.flavors
        custom_flavors = self.zen_store.list_flavors()
        return zenml_flavors + custom_flavors

    def get_flavors_by_type(
        self, component_type: "StackComponentType"
    ) -> List["FlavorResponseModel"]:
        """Fetches the list of flavor for a stack component type.

        Args:
            component_type: The type of the component to fetch.

        Returns:
            The list of flavors.
        """
        logger.debug(f"Fetching the flavors of type {component_type}.")

        from zenml.stack.flavor_registry import flavor_registry

        zenml_flavors = flavor_registry.get_flavors_by_type(
            component_type=component_type
        )

        custom_flavors = self.zen_store.list_flavors(
            project_name_or_id=self.active_project.id,
            component_type=component_type,
        )

        return zenml_flavors + custom_flavors

    def get_flavor_by_name_and_type(
        self, name: str, component_type: "StackComponentType"
    ) -> "FlavorResponseModel":
        """Fetches a registered flavor.

        Args:
            component_type: The type of the component to fetch.
            name: The name of the flavor to fetch.

        Returns:
            The registered flavor.

        Raises:
            KeyError: If no flavor exists for the given type and name.
        """
        logger.debug(
            f"Fetching the flavor of type {component_type} with name {name}."
        )

        from zenml.stack.flavor_registry import flavor_registry

        try:
            zenml_flavor = flavor_registry.get_flavor_by_name_and_type(
                component_type=component_type,
                name=name,
            )
        except KeyError:
            zenml_flavor = None

        custom_flavors = self.zen_store.list_flavors(
            project_name_or_id=self.active_project.id,
            component_type=component_type,
            name=name,
        )

        if custom_flavors:
            if len(custom_flavors) > 1:
                raise KeyError(
                    f"More than one flavor with name {name} and type "
                    f"{component_type} exists."
                )

            if zenml_flavor:
                # If there is one, check whether the same flavor exists as
                # a ZenML flavor to give out a warning
                logger.warning(
                    f"There is a custom implementation for the flavor "
                    f"'{name}' of a {component_type}, which is currently "
                    f"overwriting the same flavor provided by ZenML."
                )
            return custom_flavors[0]
        else:
            if zenml_flavor:
                return zenml_flavor
            else:
                raise KeyError(
                    f"No flavor with name '{name}' and type '{component_type}' "
                    "exists."
                )

    # -------------
    # - PIPELINES -
    # -------------

    def register_pipeline(
        self,
        pipeline_name: str,
        pipeline_spec: "PipelineSpec",
        pipeline_docstring: Optional[str],
    ) -> UUID:
        """Registers a pipeline in the ZenStore within the active project.

        This will do one of the following three things:
        A) If there is no pipeline with this name, register a new pipeline.
        B) If a pipeline exists that has the same config, use that pipeline.
        C) If a pipeline with different config exists, raise an error.

        Args:
            pipeline_name: The name of the pipeline to register.
            pipeline_spec: The spec of the pipeline.
            pipeline_docstring: The docstring of the pipeline.

        Returns:
            The id of the existing or newly registered pipeline.

        Raises:
            AlreadyExistsException: If there is an existing pipeline in the
                project with the same name but a different configuration.
        """

        existing_pipelines = self.zen_store.list_pipelines(
            name=pipeline_name,
        )

        # A) If there is no pipeline with this name, register a new pipeline.
        if len(existing_pipelines) == 0:
            create_pipeline_request = PipelineRequestModel(
                project=self.active_project.id,
                user=self.active_user.id,
                name=pipeline_name,
                spec=pipeline_spec,
                docstring=pipeline_docstring,
            )
            pipeline = self.zen_store.create_pipeline(
                pipeline=create_pipeline_request
            )
            logger.info(f"Registered new pipeline with name {pipeline.name}.")
            return pipeline.id

        else:
            if len(existing_pipelines) == 1:
                existing_pipeline = existing_pipelines[0]
                # B) If a pipeline exists that has the same config, use that
                # pipeline.
                if pipeline_spec == existing_pipeline.spec:
                    logger.debug(
                        "Did not register pipeline since it already exists."
                    )
                    return existing_pipeline.id

        # C) If a pipeline with different config exists, raise an error.
        error_msg = (
            f"Cannot run pipeline '{pipeline_name}' since this name has "
            "already been registered with a different pipeline "
            "configuration. You have three options to resolve this issue:\n"
            "1) You can register a new pipeline by changing the name "
            "of your pipeline, e.g., via `@pipeline(name='new_pipeline_name')."
            "\n2) You can execute the current run without linking it to any "
            "pipeline by setting the 'unlisted' argument to `True`, e.g., "
            "via `my_pipeline_instance.run(unlisted=True)`. "
            "Unlisted runs are not linked to any pipeline, but are still "
            "tracked by ZenML and can be accessed via the 'All Runs' tab. \n"
            "3) You can delete the existing pipeline via "
            f"`zenml pipeline delete {pipeline_name}`. This will then "
            "change all existing runs of this pipeline to become unlisted."
        )
        raise AlreadyExistsException(error_msg)

    def list_pipelines(
        self,
        project_name_or_id: Optional[Union[str, UUID]] = None,
        user_name_or_id: Optional[Union[str, UUID]] = None,
        name: Optional[str] = None,
    ) -> List[PipelineResponseModel]:
        """List pipelines.

        Args:
            project_name_or_id: If provided, only list pipelines in this
                project.
            user_name_or_id: If provided, only list pipelines from this user.
            name: If provided, only list pipelines with this name.

        Raises:
            KeyError: If the name_id_or_prefix does not uniquely identify one
                pipeline
        """

        return self.zen_store.list_pipelines(
            project_name_or_id=project_name_or_id,
            user_name_or_id=user_name_or_id,
            name=name,
        )

    def get_pipeline(self, name_id_or_prefix: str) -> PipelineResponseModel:
        """List pipelines.

        Args:
            name_id_or_prefix: ID of the pipeline.

        Raises:
            KeyError: If the name_id_or_prefix does not uniquely identify one
                pipeline
        """

        return self._get_entity_by_id_or_name_or_prefix(
            response_model=PipelineResponseModel,
            get_method=self.zen_store.get_pipeline,
            list_method=self.zen_store.list_pipelines,
            name_id_or_prefix=name_id_or_prefix,
        )

    def delete_pipeline(self, name_id_or_prefix: Union[str, UUID]) -> None:
        """Delete a pipeline.

        Args:
            name_id_or_prefix: The name, id or prefix id of the pipeline
                to delete.

        Raises:
            KeyError: If the name_id_or_prefix does not uniquely identify one
                pipeline
        """

        pipeline = self.get_pipeline(name_id_or_prefix=name_id_or_prefix)
        self.zen_store.delete_pipeline(pipeline_id=pipeline.id)

    # -----------------
    # - PIPELINE RUNS -
    # -----------------

    def export_pipeline_runs(self, filename: str) -> None:
        """Export all pipeline runs to a YAML file.

        Args:
            filename: The filename to export the pipeline runs to.
        """
        import json

        from zenml.utils.yaml_utils import write_yaml

        pipeline_runs = self.zen_store.list_runs(
            project_name_or_id=self.active_project.id
        )
        if not pipeline_runs:
            logger.warning("No pipeline runs found. Nothing to export.")
            return
        yaml_data = []
        for pipeline_run in pipeline_runs:
            run_dict = json.loads(pipeline_run.json())
            run_dict["steps"] = []
            steps = self.zen_store.list_run_steps(run_id=pipeline_run.id)
            for step in steps:
                step_dict = json.loads(step.json())
                step_dict["output_artifacts"] = []
                artifacts = self.zen_store.list_artifacts(
                    parent_step_id=step.id
                )
                for artifact in sorted(artifacts, key=lambda x: x.created):
                    artifact_dict = json.loads(artifact.json())
                    step_dict["output_artifacts"].append(artifact_dict)
                run_dict["steps"].append(step_dict)
            yaml_data.append(run_dict)
        write_yaml(filename, yaml_data)
        logger.info(f"Exported {len(yaml_data)} pipeline runs to {filename}.")

    def import_pipeline_runs(self, filename: str) -> None:
        """Import pipeline runs from a YAML file.

        Args:
            filename: The filename from which to import the pipeline runs.
        """
        from datetime import datetime

        from zenml.utils.yaml_utils import read_yaml

        step_id_mapping: Dict[str, UUID] = {}
        artifact_id_mapping: Dict[str, UUID] = {}
        yaml_data = read_yaml(filename)
        for pipeline_run_dict in yaml_data:
            steps = pipeline_run_dict.pop("steps")
            pipeline_run_dict.pop("id")
            pipeline_run = PipelineRunRequestModel.parse_obj(pipeline_run_dict)
            pipeline_run.updated = datetime.now()
            pipeline_run.user = self.active_user.id
            pipeline_run.project = self.active_project.id
            pipeline_run.stack = None
            pipeline_run.pipeline = None
            pipeline_run.mlmd_id = None
            pipeline_run = self.zen_store.create_run(pipeline_run)
            for step_dict in steps:
                artifacts = step_dict.pop("output_artifacts")
                step_id = step_dict.pop("id")
                step = StepRunRequestModel.parse_obj(step_dict)
                step.pipeline_run_id = pipeline_run.id
                step.parent_step_ids = [
                    step_id_mapping[str(parent_step_id)]
                    for parent_step_id in step.parent_step_ids
                ]
                step.input_artifacts = {
                    input_name: artifact_id_mapping[str(artifact_id)]
                    for input_name, artifact_id in step.input_artifacts.items()
                }
                step.updated = datetime.now()
                step.mlmd_id = None
                step.mlmd_parent_step_ids = []
                step = self.zen_store.create_run_step(step)
                step_id_mapping[str(step_id)] = step.id
                for artifact_dict in artifacts:
                    artifact_id = artifact_dict.pop("id")
                    artifact = ArtifactRequestModel.parse_obj(artifact_dict)
                    artifact.parent_step_id = step.id
                    artifact.producer_step_id = step_id_mapping[
                        str(artifact.producer_step_id)
                    ]
                    artifact.updated = datetime.now()
                    artifact.mlmd_id = None
                    artifact.mlmd_parent_step_id = None
                    artifact.mlmd_producer_step_id = None
                    artifact = self.zen_store.create_artifact(artifact)
                    artifact_id_mapping[str(artifact_id)] = artifact.id
        logger.info(f"Imported {len(yaml_data)} pipeline runs from {filename}.")

    def migrate_pipeline_runs(
        self,
        database: str,
        database_type: str = "sqlite",
        mysql_host: Optional[str] = None,
        mysql_port: int = 3306,
        mysql_username: Optional[str] = None,
        mysql_password: Optional[str] = None,
    ) -> None:
        """Migrate pipeline runs from a metadata store of ZenML < 0.20.0.

        Args:
            database: The metadata store database from which to migrate the
                pipeline runs. Either a path to a SQLite database or a database
                name for a MySQL database.
            database_type: The type of the metadata store database
                ("sqlite" | "mysql"). Defaults to "sqlite".
            mysql_host: The host of the MySQL database.
            mysql_port: The port of the MySQL database. Defaults to 3306.
            mysql_username: The username of the MySQL database.
            mysql_password: The password of the MySQL database.

        Raises:
            NotImplementedError: If the database type is not supported.
            RuntimeError: If no pipeline runs exist.
            ValueError: If the database type is "mysql" but the MySQL host,
                username or password are not provided.
        """
        from tfx.orchestration import metadata

        from zenml.enums import ExecutionStatus
        from zenml.zen_stores.metadata_store import MetadataStore

        # Define MLMD connection config based on the database type.
        if database_type == "sqlite":
            mlmd_config = metadata.sqlite_metadata_connection_config(database)
        elif database_type == "mysql":
            if not mysql_host or not mysql_username or mysql_password is None:
                raise ValueError(
                    "Migration from MySQL requires username, password and host "
                    "to be set."
                )
            mlmd_config = metadata.mysql_metadata_connection_config(
                database=database,
                host=mysql_host,
                port=mysql_port,
                username=mysql_username,
                password=mysql_password,
            )
        else:
            raise NotImplementedError(
                "Migrating pipeline runs is only supported for SQLite and "
                "MySQL."
            )

        metadata_store = MetadataStore(config=mlmd_config)

        # Dicts to keep tracks of MLMD IDs, which we need to resolve later.
        step_mlmd_id_mapping: Dict[int, UUID] = {}
        artifact_mlmd_id_mapping: Dict[int, UUID] = {}

        # Get all pipeline runs from the metadata store.
        pipeline_runs = metadata_store.get_all_runs()
        if not pipeline_runs:
            raise RuntimeError("No pipeline runs found in the metadata store.")

        # For each run, first store the pipeline run, then all steps, then all
        # output artifacts of each step.
        # Runs, steps, and artifacts need to be sorted chronologically ensure
        # that the MLMD IDs of producer steps and parent steps can be resolved.
        for mlmd_run in sorted(pipeline_runs, key=lambda x: x.mlmd_id):
            steps = metadata_store.get_pipeline_run_steps(
                mlmd_run.mlmd_id
            ).values()

            # Mark all steps that haven't finished yet as failed.
            step_statuses = []
            for step in steps:
                status = metadata_store.get_step_status(step.mlmd_id)
                if status == ExecutionStatus.RUNNING:
                    status = ExecutionStatus.FAILED
                step_statuses.append(status)

            num_steps = len(steps)
            pipeline_run = PipelineRunRequestModel(
                user=self.active_user.id,  # Old user might not exist.
                project=self.active_project.id,  # Old project might not exist.
                name=mlmd_run.name,
                stack=None,  # Stack might not exist in new DB.
                pipeline=None,  # Pipeline might not exist in new DB.
                status=ExecutionStatus.run_status(step_statuses, num_steps),
                pipeline_configuration=mlmd_run.pipeline_configuration,
                num_steps=num_steps,
                mlmd_id=None,  # Run might not exist in new MLMD.
            )
            new_run = self.zen_store.create_run(pipeline_run)
            for step, step_status in sorted(
                zip(steps, step_statuses), key=lambda x: x[0].mlmd_id
            ):
                parent_step_ids = [
                    step_mlmd_id_mapping[mlmd_parent_step_id]
                    for mlmd_parent_step_id in step.mlmd_parent_step_ids
                ]
                inputs = metadata_store.get_step_input_artifacts(
                    step_id=step.mlmd_id,
                    step_parent_step_ids=step.mlmd_parent_step_ids,
                )
                outputs = metadata_store.get_step_output_artifacts(
                    step_id=step.mlmd_id
                )
                input_artifacts = {
                    input_name: artifact_mlmd_id_mapping[mlmd_artifact.mlmd_id]
                    for input_name, mlmd_artifact in inputs.items()
                }
                step_run = StepRunRequestModel(
                    name=step.name,
                    pipeline_run_id=new_run.id,
                    parent_step_ids=parent_step_ids,
                    input_artifacts=input_artifacts,
                    status=step_status,
                    entrypoint_name=step.entrypoint_name,
                    parameters=step.parameters,
                    step_configuration={},
                    mlmd_parent_step_ids=[],
                    num_outputs=len(outputs),
                )
                new_step = self.zen_store.create_run_step(step_run)
                step_mlmd_id_mapping[step.mlmd_id] = new_step.id
                for output_name, mlmd_artifact in sorted(
                    outputs.items(), key=lambda x: x[1].mlmd_id
                ):
                    producer_step_id = step_mlmd_id_mapping[
                        mlmd_artifact.mlmd_producer_step_id
                    ]
                    artifact = ArtifactRequestModel(
                        name=output_name,
                        parent_step_id=new_step.id,
                        producer_step_id=producer_step_id,
                        type=mlmd_artifact.type,
                        uri=mlmd_artifact.uri,
                        materializer=mlmd_artifact.materializer,
                        data_type=mlmd_artifact.data_type,
                        is_cached=mlmd_artifact.is_cached,
                    )
                    new_artifact = self.zen_store.create_artifact(artifact)
                    artifact_mlmd_id_mapping[
                        mlmd_artifact.mlmd_id
                    ] = new_artifact.id
        logger.info(f"Migrated {len(pipeline_runs)} pipeline runs.")

    def list_runs(
        self,
        project_name_or_id: Optional[Union[str, UUID]] = None,
        stack_id: Optional[UUID] = None,
        component_id: Optional[UUID] = None,
        run_name: Optional[str] = None,
        user_name_or_id: Optional[Union[str, UUID]] = None,
        pipeline_id: Optional[UUID] = None,
        unlisted: bool = False,
    ) -> List[PipelineRunResponseModel]:
        """Gets all pipeline runs.

        Args:
            project_name_or_id: If provided, only return runs for this project.
            stack_id: If provided, only return runs for this stack.
            component_id: Optionally filter for runs that used the
                          component
            run_name: Run name if provided
            user_name_or_id: If provided, only return runs for this user.
            pipeline_id: If provided, only return runs for this pipeline.
            unlisted: If True, only return unlisted runs that are not
                associated with any pipeline (filter by `pipeline_id==None`).

        Returns:
            A list of all pipeline runs.
        """
        return self.zen_store.list_runs(
            project_name_or_id=project_name_or_id,
            stack_id=stack_id,
            component_id=component_id,
            run_name=run_name,
            user_name_or_id=user_name_or_id,
            pipeline_id=pipeline_id,
            unlisted=unlisted,
        )

    def get_pipeline_run(
        self,
        name_id_or_prefix: Union[str, UUID],
    ) -> PipelineRunResponseModel:
        """List pipelines.

        Args:
            name_id_or_prefix: ID of the pipeline run.

        Raises:
            KeyError: If the name_id_or_prefix does not uniquely identify one
                pipeline
        """

        return self._get_entity_by_id_or_name_or_prefix(
            response_model=PipelineRunResponseModel,
            get_method=self.zen_store.get_run,
            list_method=self.zen_store.list_runs,
            name_id_or_prefix=name_id_or_prefix,
        )

    # ---- utility prefix matching get functions -----

    # TODO: This prefix matching functionality should be moved to the
    #   corresponding SQL ZenStore list methods

    def _get_component_by_id_or_name_or_prefix(
        self,
        name_id_or_prefix: str,
        component_type: StackComponentType,
    ) -> "ComponentResponseModel":
        """Fetches a component of given type using the name, id or partial id.

        Args:
            name_id_or_prefix: The id, name or partial id of the component to
                fetch.
            component_type: The type of the component to fetch.

        Returns:
            The component with the given name.

        Raises:
            KeyError: If no stack with the given name exists.
        """
        # First interpret as full UUID
        try:
            component_id = UUID(str(name_id_or_prefix))
            return self.zen_store.get_stack_component(component_id)
        except ValueError:
            pass

        components = self.zen_store.list_stack_components(
            name=name_id_or_prefix,
            type=component_type,
        )

        if len(components) > 1:
            raise KeyError(
                f"Multiple {component_type.value} components have been found "
                f"for name '{name_id_or_prefix}'. The components listed "
                f"above all share this name. Please specify the component by "
                f"full or partial id."
            )
        elif len(components) == 1:
            return components[0]
        else:
            logger.debug(
                f"No component with name '{name_id_or_prefix}' "
                f"exists. Trying to resolve as partial_id"
            )

            filtered_comps = [
                component
                for component in components
                if str(component.id).startswith(name_id_or_prefix)
            ]
            if len(filtered_comps) > 1:
                raise KeyError(
                    f"The components listed above all share the provided "
                    f"prefix '{name_id_or_prefix}' on their ids. Please "
                    f"provide more characters to uniquely identify only one "
                    f"component."
                )

            elif len(filtered_comps) == 1:
                return filtered_comps[0]
            else:
                raise KeyError(
                    f"No component of type `{component_type}` with name or id "
                    f"prefix '{name_id_or_prefix}' exists."
                )

    def _get_entity_by_id_or_name_or_prefix(
        self,
        response_model: Type[AnyResponseModel],
        get_method: Callable,
        list_method: Callable,
        name_id_or_prefix: Union[str, UUID],
    ) -> "AnyResponseModel":
        """Fetches an entity using the name, id or partial id.

        Args:
            name_id_or_prefix: The id, name or partial id of the entity to
                fetch.

        Returns:
            The entity with the given name, id or partial id.

        Raises:
            KeyError: If no entity with the given name exists.
        """
        # First interpret as full UUID
        try:
            if isinstance(name_id_or_prefix, UUID):
                return get_method(name_id_or_prefix)
            else:
                entity_id = UUID(name_id_or_prefix)
                return get_method(entity_id)
        except ValueError:
            pass

        if "project" in response_model.__fields__:
            entities: List[AnyResponseModel] = list_method(
                name=name_id_or_prefix,
                project_name_or_id=self.active_project.id,
            )
        else:
            entities: List[AnyResponseModel] = list_method(
                name=name_id_or_prefix,
            )

        if len(entities) > 1:
            raise KeyError(
                f"Multiple {response_model} have been found "
                f"for name '{name_id_or_prefix}'. The {response_model} listed "
                f"above all share this name. Please specify by "
                f"full or partial id."
            )
        elif len(entities) == 1:
            return entities[0]
        else:
            logger.debug(
                f"No {response_model} with name '{name_id_or_prefix}' "
                f"exists. Trying to resolve as partial_id"
            )

            filtered_entities = [
                entity
                for entity in entities
                if str(entity.id).startswith(name_id_or_prefix)  # type: ignore[arg-type]
            ]
            if len(filtered_entities) > 1:
                raise KeyError(
                    f"The {response_model} listed above all share the provided "
                    f"prefix '{name_id_or_prefix}' on their ids. Please "
                    f"provide more characters to uniquely identify only one "
                    f"{response_model}."
                )

            elif len(filtered_entities) == 1:
                return filtered_entities[0]
            else:
                raise KeyError(
                    f"No {response_model} with name or id "
                    f"prefix '{name_id_or_prefix}' exists."
                )

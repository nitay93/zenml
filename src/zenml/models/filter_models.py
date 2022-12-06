from __future__ import annotations
from abc import ABC, abstractmethod

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Type, Union, List, Optional, ClassVar, get_args
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, validator, root_validator, PrivateAttr, Field
from sqlmodel import SQLModel

from zenml.utils.enum_utils import StrEnum
from zenml.logger import get_logger

logger = get_logger(__name__)

# ------------------ #
# QUERY PARAM MODELS #
# ------------------ #


@dataclass
class RawParams:
    limit: int
    offset: int


class GenericFilterOps(StrEnum):
    """Ops for all filters for string values on list methods"""

    EQUALS = "equals"
    CONTAINS = "contains"
    STARTSWITH = "startswith"
    ENDSWITH = "endswith"
    GTE = "gte"
    GT = "gt"
    LTE = "lte"
    LT = "lt"


class Filter(BaseModel, ABC):
    operation: GenericFilterOps
    column: str
    value: Any

    @abstractmethod
    def generate_query_conditions(
        self,
        table: Type[SQLModel],
    ):
        """Generate the query conditions for the database.

        Args:
            table: The SQLModel table to use for the query creation

        Returns:
            A list of conditions that will be combined using the `and` operation
        """
        pass


class BoolFilter(Filter):
    ALLOWED_OPS: ClassVar[List[str]] = [
        GenericFilterOps.EQUALS,
    ]
    def generate_query_conditions(
        self,
        table: Type[SQLModel],
    ):
        """Generate the query conditions for the database.

        Args:
            table: The SQLModel table to use for the query creation

        Returns:
            A list of conditions that will be combined using the `and` operation
        """
        if self.operation == GenericFilterOps.EQUALS:
            return getattr(table, self.column) == self.value


class StrFilter(Filter):
    ALLOWED_OPS: ClassVar[List[str]] = [
        GenericFilterOps.EQUALS,
        GenericFilterOps.STARTSWITH,
        GenericFilterOps.CONTAINS,
        GenericFilterOps.ENDSWITH,
    ]

    def generate_query_conditions(
        self,
        table: Type[SQLModel],
    ):
        """Generate the query conditions for the database.

        Args:
            table: The SQLModel table to use for the query creation

        Returns:
            A list of conditions that will be combined using the `and` operation
        """
        if self.operation == GenericFilterOps.EQUALS:
            return getattr(table, self.column) == self.value
        elif self.operation == GenericFilterOps.CONTAINS:
            return getattr(table, self.column).like(f"%{self.value}%")
        elif self.operation == GenericFilterOps.STARTSWITH:
            return getattr(table, self.column).startswith(f"%{self.value}%")
        elif self.operation == GenericFilterOps.CONTAINS:
            return getattr(table, self.column).endswith(f"%{self.value}%")


class UUIDFilter(Filter):
    ALLOWED_OPS: ClassVar[List[str]] = [
        GenericFilterOps.EQUALS,
        GenericFilterOps.STARTSWITH,
        GenericFilterOps.CONTAINS,
        GenericFilterOps.ENDSWITH,
    ]
    def generate_query_conditions(
        self,
        table: Type[SQLModel],
    ):
        """Generate the query conditions for the database.

        Args:
            table: The SQLModel table to use for the query creation

        Returns:
            A list of conditions that will be combined using the `and` operation
        """
        from sqlalchemy_utils.functions import cast_if
        import sqlalchemy

        if self.operation == GenericFilterOps.EQUALS:
            return getattr(table, self.column) == self.value
        elif self.operation == GenericFilterOps.CONTAINS:
            return (cast_if(getattr(table, self.column), sqlalchemy.String)
                    .like(f"%{self.value}%"))
        elif self.operation == GenericFilterOps.STARTSWITH:
            return (cast_if(getattr(table, self.column), sqlalchemy.String)
                    .startswith(f"%{self.value}%"))
        elif self.operation == GenericFilterOps.CONTAINS:
            return (cast_if(getattr(table, self.column), sqlalchemy.String)
                    .endswith(f"%{self.value}%"))


class NumericFilter(Filter):
    ALLOWED_OPS: ClassVar[List[str]] = [
        GenericFilterOps.EQUALS,
        GenericFilterOps.GT,
        GenericFilterOps.GTE,
        GenericFilterOps.LT,
        GenericFilterOps.LTE,
    ]
    def generate_query_conditions(
        self,
        table: Type[SQLModel],
    ):
        """Generate the query conditions for the database.

        Args:
            table: The SQLModel table to use for the query creation

        Returns:
            A list of conditions that will be combined using the `and` operation
        """
        if self.operation == GenericFilterOps.EQUALS:
            return getattr(table, self.column) == self.value
        elif self.operation == GenericFilterOps.GTE:
            return getattr(table, self.column) >= self.value
        elif self.operation == GenericFilterOps.GT:
            return getattr(table, self.column) > self.value
        elif self.operation == GenericFilterOps.LTE:
            return getattr(table, self.column) <= self.value
        elif self.operation == GenericFilterOps.LT:
            return getattr(table, self.column) < self.value


class ListBaseModel(BaseModel):
    """Class to unify all filter, paginate and sort request parameters in one place.

    This Model allows fine-grained filtering, sorting and pagination of
    resources.

    Usage for a given Child of this class:
    ```
    ResourceListModel(
        name="contains:default",
        project="default"
        count_steps="gte:5"
        sort_by="created",
        page=2,
        size=50
    )
    ```
    """
    list_of_filters: List[Filter] = Field(None, exclude=True)

    sort_by: str = Query("created")

    page: int = Query(1, ge=1, description="Page number")
    size: int = Query(50, ge=1, le=100, description="Page size")

    id: Union[UUID, str] = Query(None, description="Id for this resource")
    created: Union[datetime, str] = Query(None, description="Created")
    updated: Union[datetime, str] = Query(None, description="Updated")

    class Config:
        extras = False
        fields = {'list_of_filters': {'exclude': True}}

    @validator("sort_by", pre=True)
    def sort_column(cls, v):
        if v in ["sort_by", "_list_of_filters", "page", "size"]:
            raise ValueError(
                f"This resource can not be sorted by this field: '{v}'"
            )
        elif v in cls.__fields__:
            return v
        else:
            raise ValueError(
                "You can only sort by valid fields of this resource"
            )

    @root_validator(pre=True)
    def filter_ops(cls, values):
        """Parse incoming filters to extract the operations on each value."""
        list_of_filters = []

        # These 3 fields do not represent filter fields
        exclude_fields = {"sort_by", "page", "size"}

        for key, value in values.items():
            if key in exclude_fields:
                pass
            elif value:
                operator = GenericFilterOps.EQUALS

                if isinstance(value, str):
                    split_value = value.split(":", 1)
                    if (
                        len(split_value) == 2
                        and split_value[0] in GenericFilterOps.values()
                    ):
                        value = split_value[1]
                        operator = GenericFilterOps(split_value[0])

                if issubclass(datetime, get_args(cls.__fields__[key].type_)):
                    try:
                        supported_format = '%y-%m-%d %H:%M:%S'
                        datetime_value = datetime.strptime(value,
                                                           supported_format)
                    except ValueError as e:
                        raise ValueError("The datetime filter only works with "
                                         "value in the following format is "
                                         "expected: `{supported_format}`"
                                         ) from e

                    list_of_filters.append(
                        NumericFilter(
                            operation=GenericFilterOps(operator),
                            column=key,
                            value=datetime_value,
                        )
                    )
                elif issubclass(UUID, get_args(cls.__fields__[key].type_)):
                    if (operator == GenericFilterOps.EQUALS
                            and not isinstance(value, UUID)):
                        try:
                            value = UUID(value)
                        except ValueError as e:
                            raise ValueError("Invalid value passed as UUID as "
                                             "query parameter.") from 3
                    elif operator != GenericFilterOps.EQUALS:
                        value = str(value)

                    list_of_filters.append(
                        UUIDFilter(
                            operation=GenericFilterOps(operator),
                            column=key,
                            value=value,
                        )
                    )
                elif issubclass(int, get_args(cls.__fields__[key].type_)):
                    list_of_filters.append(
                        NumericFilter(
                            operation=GenericFilterOps(operator),
                            column=key,
                            value=int(value),
                        )
                    )
                elif issubclass(bool, get_args(cls.__fields__[key].type_)):
                    list_of_filters.append(
                        BoolFilter(
                            operation=GenericFilterOps(operator),
                            column=key,
                            value=bool(value),
                        )
                    )
                elif (issubclass(str, get_args(cls.__fields__[key].type_))
                        or cls.__fields__[key].type_ == str):
                    list_of_filters.append(
                        StrFilter(
                            operation=GenericFilterOps(operator),
                            column=key,
                            value=value,
                        )
                    )
                else:
                    logger.warning("The Datatype "
                                   "cls.__fields__[key].type_ might "
                                   "not be supported for filtering ")
                    list_of_filters.append(
                        StrFilter(
                            operation=GenericFilterOps(operator),
                            column=key,
                            value=str(value),
                        )
                    )

        values["list_of_filters"] = list_of_filters
        return values

    def get_pagination_params(self) -> RawParams:
        return RawParams(
            limit=self.size,
            offset=self.size * (self.page - 1),
        )

    def generate_filter(self, table: Type[SQLModel]):
        ands = []
        for column_filter in self.list_of_filters:
            ands.append(column_filter.generate_query_conditions(table=table))

        return ands

    @classmethod
    def click_list_options(cls):
        import click

        options = list()
        for k, v in cls.__fields__.items():
            if k not in ["list_of_filters"]:
                options.append(
                    click.option(
                        f"--{k}",
                        type=str,
                        default=v.default,
                        required=False,
                    )
                )

        def wrapper(function):
            for option in reversed(options):
                function = option(function)
            return function

        return wrapper

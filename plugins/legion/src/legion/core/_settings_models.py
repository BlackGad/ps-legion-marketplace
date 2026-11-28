import re
from typing import (
    Annotated,
    Literal,
    Optional,
    Self,
    TypeVar,
    Union,
)

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic.json_schema import JsonSchemaValue


def _unique(items: list[str]) -> list[str]:
    if len(set(items)) < len(items):
        raise ValueError("items must be unique")
    return items


# Apply constraints on the list before checking its uniqueness.
_StringList = TypeVar("_StringList", bound=list[str])
UniqueItems = Annotated[_StringList, Field(json_schema_extra={"uniqueItems": True}), AfterValidator(_unique)]


def _regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError(f"not a regular expression: {error}") from None
    return pattern


Regex = Annotated[str, Field(json_schema_extra={"format": "regex"}), AfterValidator(_regex)]


def _named_command(words: list[str]) -> list[str]:
    if not words[0]:
        raise ValueError("the first word must not be empty")
    return words


def _command_schema(schema: JsonSchemaValue) -> None:
    # Draft-07 tuple validation lets us constrain the first array item.
    items = schema["items"]
    schema["items"] = [{**items, "minLength": 1}]
    schema["additionalItems"] = items


Command = Annotated[list[str], Field(min_length=1, json_schema_extra=_command_schema), AfterValidator(_named_command)]


def _schema_version_const(schema: JsonSchemaValue) -> None:
    schema["const"] = schema.pop("minimum")
    schema.pop("maximum")


# Account, tool and channel ids; an id names files under the legion home, so it cannot hold a path.
NAME_PATTERN = r"[a-z][a-z0-9-]*"
Name = Annotated[str, Field(pattern=f"^{NAME_PATTERN}$")]
Text = Annotated[str, Field(min_length=1)]
# A Literal[1] would accept true; JSON Schema's const 1 does not.
_SCHEMA_VERSION = 1
SchemaVersion = Annotated[int, Field(ge=_SCHEMA_VERSION, le=_SCHEMA_VERSION, json_schema_extra=_schema_version_const)]


def _settings_schema(schema: JsonSchemaValue, model: type["SettingsModel"]) -> None:
    properties = schema["properties"]
    for field in model.model_fields.values():
        if field.is_required():
            continue
        property_schema = properties[field.alias]
        choices = property_schema.get("anyOf")
        if choices is None:
            continue
        non_null = [choice for choice in choices if choice.get("type") != "null"]
        if len(non_null) == len(choices):
            continue
        if property_schema.get("default") is None:
            property_schema.pop("default", None)
        if len(non_null) == 1:
            del property_schema["anyOf"]
            property_schema.update(non_null[0])
        else:
            property_schema["anyOf"] = non_null


class SettingsModel(BaseModel):
    # A legion JSON file: camelCase property names, no unknown properties, exact JSON types.
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid", strict=True, json_schema_extra=_settings_schema)

    # A property with a default may be omitted. If supplied, it must have a value.
    @model_validator(mode="before")
    @classmethod
    def _optional_is_not_null(cls, data: object) -> object:
        if isinstance(data, dict):
            for name, field in cls.model_fields.items():
                key = field.alias or name
                if not field.is_required() and key in data and data[key] is None:
                    raise ValueError(f"'{key}' must not be null")
        return data


class Deployment(SettingsModel):
    name: str = Field(description="Unique deployment name.")
    model: Annotated[Optional[str], Field(description="Underlying model name when discovery knows it.")] = None
    alias: Annotated[Optional[Text], Field(description="Optional unique model alias supported by the tool.")] = None


class Provider(SettingsModel):
    kind: Text
    endpoint: str = Field(pattern=r"^https://[^/]+$", description="HTTPS endpoint scheme and host, without a path or trailing slash.")
    default: str = Field(description="Default deployment name from deployments.")
    deployments: Annotated[list[Deployment], Field(min_length=1)]


class Agent(SettingsModel):
    id: Name = Field(description="Unique lowercase hyphenated account id.")
    tool: Name = Field(description="CLI provider.")
    config_dir: Text = Field(description="Concrete account configuration folder. New accounts use ~/.legion/agents/<id>/.")
    command: Annotated[Optional[Text], Field(description="The account's own CLI program, only when it is not the tool's stock command. Subscription accounts only.")] = None
    provider: Optional[Provider] = None


class Channel(SettingsModel):
    id: Name
    kind: Name
    target: Text
    accounts: UniqueItems[Annotated[list[Name], Field(min_length=1)]]


class PersonalisationSettings(SettingsModel):
    schema_uri: Optional[str] = Field(None, alias="$schema")
    schema_version: SchemaVersion
    agents: list[Agent]
    channels: list[Channel]
    knowledge_enabled: Optional[bool] = None


class ProjectSettings(SettingsModel):
    schema_uri: Optional[str] = Field(None, alias="$schema")
    schema_version: SchemaVersion


class ChannelState(SettingsModel):
    variable: str
    folder: str
    remove: list[str]
    token_file: str
    setup: str


class ChannelReady(SettingsModel):
    command: str
    plugin_folder: dict[str, str]


class ChannelDescriptor(SettingsModel):
    schema_version: SchemaVersion
    id: Name
    launch: dict[str, list[str]]
    state: Optional[dict[str, ChannelState]] = None
    ready: Optional[ChannelReady] = None


# Tool descriptor patterns use look-ahead, which needs Python's regular expressions.
class ToolModel(SettingsModel):
    model_config = ConfigDict(regex_engine="python-re")


NO_PARENT = r"(?!.*(?:^|[/\\])\.\.(?:[/\\]|$))"
RelativeFolder = Annotated[str, Field(pattern=rf"^(?![/\\]|[A-Za-z]:){NO_PARENT}.*$")]
EnvironmentName = Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]*$")]


def _listing_env_schema(schema: JsonSchemaValue) -> None:
    # patternProperties alone would leave keys outside the pattern unconstrained.
    ((pattern, value_schema),) = schema.pop("patternProperties").items()
    schema["propertyNames"] = {"pattern": pattern}
    schema["additionalProperties"] = value_schema


class Foundry(ToolModel):
    deployment_prefix: Text
    aliases: UniqueItems[list[Text]]


class MarketplaceListing(ToolModel):
    format: Literal["json"]
    collection_field: Optional[Text] = None
    root_field: Optional[Text] = None
    name_field: Text


class LinesListing(ToolModel):
    format: Literal["lines"]
    reject_pattern: Regex


class JsonListing(ToolModel):
    format: Literal["json"]
    id_field: Text
    enabled_field: Text


class Plugin(ToolModel):
    default_config_dir: Optional[Annotated[str, Field(pattern=rf"^{NO_PARENT}~/[^:]+$")]] = None
    installed_root: Optional[Annotated[str, Field(pattern=rf"^(?![/\\]|[A-Za-z]:|~[/\\]){NO_PARENT}.+$")]] = None
    adopt_rewrite: Optional[UniqueItems[list[RelativeFolder]]] = None
    marketplace_add: list[str]
    marketplace_add_ref: list[str]
    marketplace_remove: list[str]
    install: list[str]
    uninstall: list[str]
    marketplace_update: list[str]
    update: list[str]
    marketplace_listing: MarketplaceListing
    listing_env: Optional[Annotated[dict[EnvironmentName, str], Field(json_schema_extra=_listing_env_schema)]] = None
    marketplace_list: list[str]
    list_command: list[str] = Field(alias="list")
    listing: Annotated[Union[LinesListing, JsonListing], Field(discriminator="format")]


class Hooks(ToolModel):
    hint: str


class Platform(ToolModel):
    needs: UniqueItems[list[Name]]
    commands: list[Command]
    docs: str


class ScriptDownload(ToolModel):
    kind: Literal["script"]
    url: Text


# Per resolver: the properties it needs, the ones it does not allow, and its verification.
_RESOLVER_RULES = {
    "release-assets": (["metadata_url", "asset_pattern"], ["asset_name", "url", "checksum_url"], "asset-digest"),
    "lts-index": (["metadata_url", "asset_name", "url", "checksum_url"], ["asset_pattern"], "checksum-file"),
}
_ZIP_KIND = "zip"
_PATH_FOLDER = "path_folder"


def _binary_download_schema(schema: JsonSchemaValue, model: type[SettingsModel]) -> None:
    _settings_schema(schema, model)
    path_folder = to_camel(_PATH_FOLDER)
    rules: list[JsonSchemaValue] = [
        {
            "if": {"properties": {"kind": {"const": _ZIP_KIND}}},
            "then": {"required": [path_folder]},
            "else": {"properties": {path_folder: False}},
        }
    ]
    for resolver, (needed, refused, verification) in _RESOLVER_RULES.items():
        rules.append(
            {
                "if": {"properties": {"resolver": {"const": resolver}}},
                "then": {
                    "required": [to_camel(name) for name in needed],
                    "properties": {"verification": {"const": verification}, **dict.fromkeys(map(to_camel, refused), False)},
                },
            }
        )
    schema["allOf"] = rules


class BinaryDownload(ToolModel):
    model_config = ConfigDict(json_schema_extra=_binary_download_schema)

    kind: Literal["msi", "exe", "zip"]
    resolver: Literal["release-assets", "lts-index"]
    metadata_url: Optional[Text] = None
    asset_pattern: Optional[Annotated[Regex, Field(min_length=1)]] = None
    asset_name: Optional[Text] = None
    url: Optional[Text] = None
    checksum_url: Optional[Text] = None
    verification: Literal["asset-digest", "checksum-file"]
    arguments: list[str]
    path_folder: Optional[RelativeFolder] = None

    @model_validator(mode="after")
    def _kind_and_resolver_rules(self) -> Self:
        given = self.model_fields_set
        if self.kind == _ZIP_KIND and _PATH_FOLDER not in given:
            raise ValueError("a zip download needs pathFolder")
        if self.kind != _ZIP_KIND and _PATH_FOLDER in given:
            raise ValueError("only a zip download has pathFolder")
        needed, refused, verification = _RESOLVER_RULES[self.resolver]
        missing = [to_camel(name) for name in needed if name not in given]
        if missing:
            raise ValueError(f"resolver '{self.resolver}' needs {', '.join(missing)}")
        extra = [to_camel(name) for name in refused if name in given]
        if extra:
            raise ValueError(f"resolver '{self.resolver}' does not allow {', '.join(extra)}")
        if self.verification != verification:
            raise ValueError(f"resolver '{self.resolver}' needs verification '{verification}'")
        return self


Download = Annotated[Union[ScriptDownload, BinaryDownload], Field(discriminator="kind")]


class WindowsDownload(ToolModel):
    msi: Download
    zip: Download


class NpmInstall(ToolModel):
    check: Text
    command: Command


class Windows(ToolModel):
    winget_id: Text
    portable_command: Annotated[RelativeFolder, Field(min_length=1)]
    npm_when_available: Optional[NpmInstall] = None
    download: WindowsDownload


# How to find and install one program; the helpers in helpers.json have no hint.
class InstallRecipe(ToolModel):
    check: Command
    windows: Optional[Windows]
    macos: Platform
    linux: Platform


class Install(InstallRecipe):
    hint: str


class ToolDescriptor(ToolModel):
    schema_version: SchemaVersion
    id: Name
    command: Text
    config_env: EnvironmentName
    foundry: Optional[Foundry]
    overseer_args: list[str]
    allow_rules: Optional[UniqueItems[list[Text]]] = None
    plugin: Plugin
    hooks: Hooks
    install: Install


class CatalogTool(ToolModel):
    id: Name
    command: Text
    config_env: EnvironmentName
    plugin: Plugin
    hooks: Hooks
    install_hint: str


class PluginPin(ToolModel):
    marketplace: Name
    plugin_id: Text
    source: Text
    tag: Text
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class ToolsCatalog(ToolModel):
    schema_version: SchemaVersion
    tools: list[CatalogTool]
    wiki: Optional[PluginPin] = None


class HelpersCatalog(RootModel[dict[Name, InstallRecipe]]):
    model_config = ConfigDict(strict=True)


class PluginManifest(BaseModel):
    name: str
    version: str

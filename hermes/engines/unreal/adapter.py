"""The Unreal Engine adapter (Unreal Engine 4.23 and later, 64-bit)."""

import re

from hermes.engines import base
from hermes.engines.unreal.agent import UNREAL_AGENT_SOURCE

# A monolithic game is one executable, <Project>-<Platform>-<Configuration>;
# a modular build loads the engine as libraries (UE4-CoreUObject.dll ...).
_RUNTIME_NAMES = re.compile(
    r"-(Win64|Win32|WinGDK|Linux|LinuxArm64)-(Shipping|Test|Development|DebugGame)(\.exe)?$"
    r"|^(UE4|UE5|UnrealEditor)-CoreUObject",
    re.IGNORECASE)

# Packages of the engine and its plugins rather than of the game: the C++
# modules of /Script/ and the content of /Engine/ and engine plugins.
_ENGINE_MODULES = (
    "CoreUObject", "Engine", "AIModule", "AnimGraphRuntime", "AnimationBudgetAllocator",
    "AnimationLocomotionLibrary", "AnimationSharing", "ACLPlugin", "ActorLayerUtilities",
    "ActorSequence", "AnalyticsBlueprintLibrary", "AppleImageUtils", "AssetRegistry", "AssetTags",
    "Audio", "AutomationUtils", "BinkMediaPlayer", "CableComponent", "Chaos", "CinematicCamera",
    "ClothingSystem", "ColorCorrectRegions", "Common", "ConsoleVariables", "Constraints",
    "ControlRig", "CustomMeshComponent", "Dataflow", "DataRegistry", "DatasmithContent",
    "DeveloperSettings", "DLSS", "EngineSettings", "EnhancedInput", "EyeTracker", "FieldSystem",
    "Foliage", "GameFeatures", "GameplayAbilities", "GameplayCameras", "GameplayTags",
    "GameplayTasks", "Geometry", "GLTF", "HeadMountedDisplay", "Houdini", "IKRig",
    "ImageWriteQueue", "ImgMedia", "InputCore", "Interchange", "InteractiveToolsFramework",
    "Json", "Landscape", "LevelSequence", "LocalizableMessage", "LocationServices", "Mass",
    "Media", "MeshDescription", "MeshModeling", "Metasound", "ModelingComponents",
    "ModelViewViewModel", "ModularGameplay", "MobilePatchingUtils", "Movie", "MRMesh",
    "MultiUserClient", "NavigationSystem", "NetCore", "Niagara", "NIS", "Online",
    "OpenColorIO", "Paper2D", "PCG", "PhysicsCore", "ProceduralMeshComponent", "PropertyPath",
    "ReplicationGraph", "ResonanceAudio", "RigVM", "SequencerScripting", "Slate",
    "SmartObjects", "StateTree", "StaticMeshDescription", "Streamline", "StructUtils",
    "Synthesis", "TemplateSequence", "TimeManagement", "TraceUtilities", "TypedElement", "UMG",
    "VariantManagerContent", "Water", "Xe",
)


class UnrealAdapter(base.EngineAdapter):
    """Unreal's reflection: packages, classes and UFunctions found in memory."""

    id = "unreal"
    display_name = "Unreal Engine"
    capabilities = frozenset({base.NAMING, base.CATALOG, base.TRACE, base.MONITOR,
                              base.METHOD_HOOKS, base.GAS})
    agent_source = UNREAL_AGENT_SOURCE
    # All its code is ordinary machine code in modules; the native trace ran
    # in EVERSPACE (UE4).
    native_tools = True
    framework_prefixes = (tuple(f"/Script/{module}" for module in _ENGINE_MODULES)
                          + ("/Engine/", "/Paper2D/", "/Niagara/", "/Wwise", "/Script/AkAudio"))

    def is_runtime_file(self, name):
        return _RUNTIME_NAMES.search(name) is not None

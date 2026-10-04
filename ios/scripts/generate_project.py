#!/usr/bin/env python3
"""Generate the small, dependency-free Xcode project from its Swift sources."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
objects = {}


def ident(name):
    return hashlib.sha1(name.encode()).hexdigest()[:24].upper()


def add(name, value):
    key = ident(name)
    objects[key] = value
    return key


def q(value):
    return json.dumps(str(value))


def array(values):
    return "(" + ", ".join(values) + ("," if values else "") + ")"


def config_list(name, settings):
    ids = []
    for kind in ("Debug", "Release"):
        values = dict(settings)
        values["SWIFT_OPTIMIZATION_LEVEL"] = "-Onone" if kind == "Debug" else "-O"
        if kind == "Debug":
            values["SWIFT_ACTIVE_COMPILATION_CONDITIONS"] = "DEBUG"
            values["ENABLE_TESTABILITY"] = "YES"
        body = " ".join(f"{key} = {q(value)};" for key, value in values.items())
        ids.append(add(name + kind, f"isa = XCBuildConfiguration; name = {kind}; buildSettings = {{{body}}};"))
    return add(name + "configs", f"isa = XCConfigurationList; buildConfigurations = {array(ids)}; defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;")


def main():
    products = []
    groups = []
    targets = []
    target_names = ["Odysseus"]
    for test in ("OdysseusTests", "OdysseusUITests"):
        if list((ROOT / test).glob("*.swift")):
            target_names.append(test)
    for name in target_names:
        is_app = name == "Odysseus"
        is_ui = name == "OdysseusUITests"
        file_refs, build_refs = [], []
        for path in sorted((ROOT / name).rglob("*.swift")):
            rel = str(path.relative_to(ROOT))
            ref = add("file:" + rel, f"isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {q(rel)}; sourceTree = SOURCE_ROOT;")
            file_refs.append(ref)
            build_refs.append(add("build:" + rel, f"isa = PBXBuildFile; fileRef = {ref};"))
        groups.append(add("group:" + name, f"isa = PBXGroup; name = {name}; children = {array(file_refs)}; sourceTree = \"<group>\";"))
        ext = "app" if is_app else "xctest"
        kind = "wrapper.application" if is_app else "wrapper.cfbundle"
        product = add("product:" + name, f"isa = PBXFileReference; explicitFileType = {kind}; path = {name}.{ext}; sourceTree = BUILT_PRODUCTS_DIR;")
        products.append(product)
        source_phase = add("sources:" + name, f"isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = {array(build_refs)}; runOnlyForDeploymentPostprocessing = 0;")
        framework_phase = add("frameworks:" + name, "isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = (); runOnlyForDeploymentPostprocessing = 0;")
        resource_refs = []
        if is_app:
            for path in sorted((ROOT / name / "Resources").glob("*")):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(ROOT))
                ref = add("resource:" + rel, f"isa = PBXFileReference; path = {q(rel)}; sourceTree = SOURCE_ROOT;")
                resource_refs.append(add("resource-build:" + rel, f"isa = PBXBuildFile; fileRef = {ref};"))
        resource_phase = add("resources:" + name, f"isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = {array(resource_refs)}; runOnlyForDeploymentPostprocessing = 0;")
        settings = {
            "PRODUCT_NAME": "$(TARGET_NAME)", "PRODUCT_BUNDLE_IDENTIFIER": "dev.afterglow.odysseus.ios" + ("" if is_app else "." + name.lower()),
            "SWIFT_VERSION": "5.0", "IPHONEOS_DEPLOYMENT_TARGET": "17.0",
            "TARGETED_DEVICE_FAMILY": "1,2", "SDKROOT": "iphoneos",
            "SUPPORTED_PLATFORMS": "iphoneos iphonesimulator", "CODE_SIGN_STYLE": "Automatic",
            "GENERATE_INFOPLIST_FILE": "NO" if is_app else "YES",
        }
        if is_app:
            settings["INFOPLIST_FILE"] = "Odysseus/Info.plist"
            settings["CODE_SIGN_ENTITLEMENTS"] = "Odysseus/Odysseus.entitlements"
        elif is_ui:
            settings["TEST_TARGET_NAME"] = "Odysseus"
        else:
            settings["TEST_HOST"] = "$(BUILT_PRODUCTS_DIR)/Odysseus.app/Odysseus"
            settings["BUNDLE_LOADER"] = "$(TEST_HOST)"
        deps = []
        if not is_app:
            proxy = add("proxy:" + name, f"isa = PBXContainerItemProxy; containerPortal = {ident('project')}; proxyType = 1; remoteGlobalIDString = {ident('target:Odysseus')}; remoteInfo = Odysseus;")
            deps.append(add("dependency:" + name, f"isa = PBXTargetDependency; target = {ident('target:Odysseus')}; targetProxy = {proxy};"))
        configs = config_list(name, settings)
        product_type = "application" if is_app else "bundle.ui-testing" if is_ui else "bundle.unit-test"
        targets.append(add("target:" + name, f"isa = PBXNativeTarget; name = {name}; productName = {name}; productReference = {product}; productType = \"com.apple.product-type.{product_type}\"; buildConfigurationList = {configs}; buildPhases = {array([source_phase, framework_phase, resource_phase])}; buildRules = (); dependencies = {array(deps)};"))
    product_group = add("products", f"isa = PBXGroup; name = Products; children = {array(products)}; sourceTree = \"<group>\";")
    main_group = add("main", f"isa = PBXGroup; children = {array(groups + [product_group])}; sourceTree = \"<group>\";")
    configs = config_list("project", {"CLANG_ENABLE_MODULES": "YES", "CLANG_ENABLE_OBJC_ARC": "YES"})
    project = add("project", f"isa = PBXProject; attributes = {{LastUpgradeCheck = 1600; BuildIndependentTargetsInParallel = YES;}}; buildConfigurationList = {configs}; compatibilityVersion = \"Xcode 14.0\"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en, Base); mainGroup = {main_group}; productRefGroup = {product_group}; projectDirPath = \"\"; projectRoot = \"\"; targets = {array(targets)};")
    directory = ROOT / "Odysseus.xcodeproj"
    directory.mkdir(exist_ok=True)
    content = "// !$*UTF8*$!\n{ archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n"
    content += "\n".join(f"{key} = {{ {value} }};" for key, value in objects.items())
    content += f"\n}}; rootObject = {project}; }}\n"
    (directory / "project.pbxproj").write_text(content)
    schemes = directory / "xcshareddata/xcschemes"
    schemes.mkdir(parents=True, exist_ok=True)
    def ref(name):
        return f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{ident("target:" + name)}" BuildableName="{name}.{"app" if name == "Odysseus" else "xctest"}" BlueprintName="{name}" ReferencedContainer="container:Odysseus.xcodeproj"/>'
    tests = "".join(f'<TestableReference skipped="NO">{ref(name)}</TestableReference>' for name in target_names[1:])
    (schemes / "Odysseus.xcscheme").write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="1600" version="1.3">
<BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{ref("Odysseus")}</BuildActionEntry></BuildActionEntries></BuildAction>
<TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="YES"><Testables>{tests}</Testables></TestAction>
<LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" allowLocationSimulation="YES"><BuildableProductRunnable runnableDebuggingMode="0">{ref("Odysseus")}</BuildableProductRunnable></LaunchAction>
<ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{ref("Odysseus")}</BuildableProductRunnable></ProfileAction>
<AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>''')
    print("Generated Odysseus.xcodeproj")


if __name__ == "__main__":
    main()

#!/bin/sh
# Builds VK_LAYER_PORTKIT_imagelog (x86_64, for Wine under Rosetta) into
# runtime/layers/imagelog. Usage: layers/imagelog/build.sh [--test]
#
# --test also builds blocktexel.c and runs it on the wine11 runtime's loader
# and MoltenVK (or the driver whose ICD manifest VK_DRIVER_FILES names), with
# the layer on.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
home=${PORTKIT_HOME:-$root}
headers_tag=vulkan-sdk-1.4.350.0   # the loader bundled with wine11 is 1.4.350
headers=$home/runtime/build/Vulkan-Headers-$headers_tag
out=$home/runtime/layers/imagelog

if [ ! -d "$headers" ]; then
    git clone -q --depth 1 --branch "$headers_tag" https://github.com/KhronosGroup/Vulkan-Headers.git "$headers"
fi
mkdir -p "$out"
clang -arch x86_64 -mmacosx-version-min=11.0 -dynamiclib -O2 -Wall -fvisibility=hidden \
    -I"$headers/include" -o "$out/libimagelog.dylib" "$here/imagelog.c"
codesign --force -s - "$out/libimagelog.dylib"
cat > "$out/imagelog.json" <<JSON
{
    "file_format_version": "1.2.0",
    "layer": {
        "name": "VK_LAYER_PORTKIT_imagelog",
        "type": "GLOBAL",
        "library_path": "./libimagelog.dylib",
        "api_version": "1.4.350",
        "implementation_version": "1",
        "description": "Logs vkCreateImage / vkCreateImageView (Porting Kit)",
        "functions": {
            "vkNegotiateLoaderLayerInterfaceVersion": "imagelog_NegotiateLoaderLayerInterfaceVersion",
            "vkGetInstanceProcAddr": "imagelog_GetInstanceProcAddr",
            "vkGetDeviceProcAddr": "imagelog_GetDeviceProcAddr"
        }
    }
}
JSON
echo "$out"

if [ "${1:-}" = --test ]; then
    wine="$home/runtime/wine11-11.21/Wine Staging.app/Contents/Resources/wine"
    clang -arch x86_64 -O1 -Wall -I"$headers/include" -o "$out/blocktexel" "$here/blocktexel.c" \
        "$wine/lib/libvulkan.1.dylib"
    DYLD_FALLBACK_LIBRARY_PATH="$wine/lib" \
    VK_DRIVER_FILES=${VK_DRIVER_FILES:-$wine/share/vulkan/icd.d/MoltenVK_icd.json} \
    VK_ADD_LAYER_PATH=$out VK_INSTANCE_LAYERS=VK_LAYER_PORTKIT_imagelog \
        "$out/blocktexel"
fi

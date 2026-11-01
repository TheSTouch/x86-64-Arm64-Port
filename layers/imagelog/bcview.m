/*
 * bcview: can Metal on this Mac view a block-compressed texture through an
 * uncompressed format of the same block size (BC1 as RG32Uint, BC5 as
 * RGBA32Uint), and do reads and writes through the view reach the texture's
 * blocks? That is what Unreal's runtime virtual textures do on Vulkan
 * (VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT), and what MoltenVK
 * refuses up front. Exit status 0 when both directions work.
 *
 *   clang -fobjc-arc -framework Metal -framework Foundation bcview.m -o bcview
 *   MTL_DEBUG_LAYER=1 ./bcview
 */

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

static const char *kernels =
    "#include <metal_stdlib>\n"
    "using namespace metal;\n"
    "kernel void readview(texture2d<uint, access::read> v [[texture(0)]], device uint4 *out [[buffer(0)]],\n"
    "                     uint2 id [[thread_position_in_grid]]) {\n"
    "    out[id.y * v.get_width() + id.x] = v.read(id);\n"
    "}\n"
    "kernel void writeview(texture2d<uint, access::write> v [[texture(0)]], uint2 id [[thread_position_in_grid]]) {\n"
    "    v.write(uint4(0x11111111u * (id.x + 1), 0x01010101u * (id.y + 1), 0xabcdef00u + id.x, 0x12345600u + id.y), id);\n"
    "}\n";

static int check(id<MTLDevice> dev, id<MTLLibrary> lib, MTLPixelFormat bc, MTLPixelFormat uint_format,
                 NSUInteger block_bytes, const char *name) {
    const NSUInteger size = 16, blocks = size / 4;
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:bc width:size height:size
                                                                            mipmapped:NO];
    d.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite | MTLTextureUsagePixelFormatView;
    d.storageMode = MTLStorageModeShared;
    id<MTLTexture> tex = [dev newTextureWithDescriptor:d];
    uint8_t src[16 * 16];
    for (NSUInteger i = 0; i < sizeof src; i++) src[i] = (uint8_t)(i * 7 + 3);
    [tex replaceRegion:MTLRegionMake2D(0, 0, size, size) mipmapLevel:0 withBytes:src bytesPerRow:blocks * block_bytes];

    id<MTLTexture> view = [tex newTextureViewWithPixelFormat:uint_format];
    if (!view) {
        printf("%s: newTextureViewWithPixelFormat returned nil\n", name);
        return 1;
    }
    printf("%s: view %lux%lu\n", name, (unsigned long)view.width, (unsigned long)view.height);

    id<MTLCommandQueue> q = [dev newCommandQueue];
    id<MTLBuffer> out = [dev newBufferWithLength:blocks * blocks * 16 options:MTLResourceStorageModeShared];
    NSError *err = nil;
    id<MTLComputePipelineState> rd = [dev newComputePipelineStateWithFunction:[lib newFunctionWithName:@"readview"]
                                                                        error:&err];
    id<MTLCommandBuffer> cb = [q commandBuffer];
    id<MTLComputeCommandEncoder> e = [cb computeCommandEncoder];
    [e setComputePipelineState:rd];
    [e setTexture:view atIndex:0];
    [e setBuffer:out offset:0 atIndex:0];
    [e dispatchThreads:MTLSizeMake(view.width, view.height, 1) threadsPerThreadgroup:MTLSizeMake(4, 4, 1)];
    [e endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
    const uint32_t *got = out.contents;
    int read_ok = 1;
    for (NSUInteger b = 0; b < blocks * blocks; b++)
        if (memcmp(&got[b * 4], src + b * block_bytes, block_bytes)) read_ok = 0;
    printf("%s: read through view %s\n", name, read_ok ? "matches the blocks" : "DIFFERS");

    id<MTLComputePipelineState> wr = [dev newComputePipelineStateWithFunction:[lib newFunctionWithName:@"writeview"]
                                                                        error:&err];
    cb = [q commandBuffer];
    e = [cb computeCommandEncoder];
    [e setComputePipelineState:wr];
    [e setTexture:view atIndex:0];
    [e dispatchThreads:MTLSizeMake(view.width, view.height, 1) threadsPerThreadgroup:MTLSizeMake(4, 4, 1)];
    [e endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
    uint8_t back[16 * 16];
    [tex getBytes:back bytesPerRow:blocks * block_bytes fromRegion:MTLRegionMake2D(0, 0, size, size) mipmapLevel:0];
    int write_ok = 1;
    for (NSUInteger y = 0; y < blocks; y++)
        for (NSUInteger x = 0; x < blocks; x++) {
            uint32_t want[4] = {0x11111111u * (uint32_t)(x + 1), 0x01010101u * (uint32_t)(y + 1),
                                0xabcdef00u + (uint32_t)x, 0x12345600u + (uint32_t)y};
            if (memcmp(back + (y * blocks + x) * block_bytes, want, block_bytes)) write_ok = 0;
        }
    printf("%s: write through view %s\n", name, write_ok ? "lands in the blocks" : "DIFFERS");
    return !(read_ok && write_ok);
}

int main(void) {
    @autoreleasepool {
        id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
        printf("%s, macOS %s\n", dev.name.UTF8String,
               [NSProcessInfo processInfo].operatingSystemVersionString.UTF8String);
        NSError *err = nil;
        id<MTLLibrary> lib = [dev newLibraryWithSource:@(kernels) options:nil error:&err];
        if (!lib) {
            printf("library: %s\n", err.localizedDescription.UTF8String);
            return 2;
        }
        int failures = check(dev, lib, MTLPixelFormatBC1_RGBA, MTLPixelFormatRG32Uint, 8, "BC1 as RG32Uint");
        failures += check(dev, lib, MTLPixelFormatBC5_RGUnorm, MTLPixelFormatRGBA32Uint, 16, "BC5 as RGBA32Uint");
        return failures;
    }
}

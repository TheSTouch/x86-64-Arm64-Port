/*
 * VK_LAYER_PORTKIT_imagelog: logs the images and image views a game asks for,
 * to find out what the Vulkan driver (MoltenVK) refuses and why.
 *
 * Wine's win32u loads the system Vulkan loader (libvulkan.1.dylib), so this
 * layer is enabled from the environment like on any Mac program:
 *
 *   VK_ADD_LAYER_PATH=<folder with imagelog.json>
 *   VK_INSTANCE_LAYERS=VK_LAYER_PORTKIT_imagelog
 *
 * Built for x86_64: Wine's Unix side runs under Rosetta. Lines go to stderr,
 * which is the run's wine.log, each starting with "imagelog:".
 *
 * IMAGELOG=all logs every image, not only those that fail or ask for views in
 * another format. IMAGELOG_STRIP_BLOCK_TEXEL=1 drops
 * VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT before the driver sees it
 * (an experiment: does the game then create such a view at all?).
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <vulkan/vk_layer.h>
#include <vulkan/vulkan.h>

#define EXPORT __attribute__((visibility("default")))

/* ---- per-instance / per-device dispatch, keyed by the loader's dispatch table ---- */

typedef struct {
    void *key;
    PFN_vkGetInstanceProcAddr gipa;
    PFN_vkDestroyInstance destroy_instance;
} instance_t;

typedef struct {
    void *key;
    PFN_vkGetDeviceProcAddr gdpa;
    PFN_vkDestroyDevice destroy_device;
    PFN_vkCreateImage create_image;
    PFN_vkDestroyImage destroy_image;
    PFN_vkCreateImageView create_image_view;
} device_t;

/* Images created with a flag that allows views in another format. */
typedef struct {
    VkImage image;
    VkFormat format;
    VkImageCreateFlags flags;
    VkImageUsageFlags usage;
    VkExtent3D extent;
} image_t;

#define MAX_INSTANCES 16
#define MAX_DEVICES 16
#define MAX_IMAGES 4096

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static instance_t instances[MAX_INSTANCES];
static device_t devices[MAX_DEVICES];
static image_t images[MAX_IMAGES];
static int log_all = -1, strip_block_texel = -1;
static unsigned long image_count, logged_count;

static void *key_of(const void *dispatchable) { return *(void **)dispatchable; }

static instance_t *find_instance(const void *h) {
    void *k = key_of(h);
    for (int i = 0; i < MAX_INSTANCES; i++)
        if (instances[i].key == k) return &instances[i];
    return NULL;
}

static device_t *find_device(const void *h) {
    void *k = key_of(h);
    for (int i = 0; i < MAX_DEVICES; i++)
        if (devices[i].key == k) return &devices[i];
    return NULL;
}

static void settings(void) {
    if (log_all >= 0) return;
    const char *v = getenv("IMAGELOG");
    log_all = v && !strcmp(v, "all");
    v = getenv("IMAGELOG_STRIP_BLOCK_TEXEL");
    strip_block_texel = v && *v && strcmp(v, "0");
    fprintf(stderr, "imagelog: loaded in pid %d (all=%d strip_block_texel=%d)\n", getpid(), log_all,
            strip_block_texel);
}

/* ---- names ---- */

static const char *format_name(VkFormat f) {
    switch (f) {
#define F(x) case VK_FORMAT_##x: return #x;
        F(UNDEFINED) F(R8_UNORM) F(R8G8_UNORM) F(R8G8B8A8_UNORM) F(R8G8B8A8_SRGB) F(B8G8R8A8_UNORM)
        F(B8G8R8A8_SRGB) F(A2B10G10R10_UNORM_PACK32) F(R16_UINT) F(R16_SFLOAT) F(R16G16_UINT)
        F(R16G16_SFLOAT) F(R16G16B16A16_UINT) F(R16G16B16A16_SFLOAT) F(R32_UINT) F(R32_SFLOAT)
        F(R32G32_UINT) F(R32G32_SFLOAT) F(R32G32B32A32_UINT) F(R32G32B32A32_SFLOAT)
        F(B10G11R11_UFLOAT_PACK32) F(E5B9G9R9_UFLOAT_PACK32) F(D16_UNORM) F(D32_SFLOAT)
        F(D24_UNORM_S8_UINT) F(D32_SFLOAT_S8_UINT) F(BC1_RGB_UNORM_BLOCK) F(BC1_RGB_SRGB_BLOCK)
        F(BC1_RGBA_UNORM_BLOCK) F(BC1_RGBA_SRGB_BLOCK) F(BC2_UNORM_BLOCK) F(BC2_SRGB_BLOCK)
        F(BC3_UNORM_BLOCK) F(BC3_SRGB_BLOCK) F(BC4_UNORM_BLOCK) F(BC4_SNORM_BLOCK) F(BC5_UNORM_BLOCK)
        F(BC5_SNORM_BLOCK) F(BC6H_UFLOAT_BLOCK) F(BC6H_SFLOAT_BLOCK) F(BC7_UNORM_BLOCK)
        F(BC7_SRGB_BLOCK)
#undef F
    default: return "?";
    }
}

static int is_compressed(VkFormat f) {
    return (f >= VK_FORMAT_BC1_RGB_UNORM_BLOCK && f <= VK_FORMAT_ASTC_12x12_SRGB_BLOCK) ||
           (f >= VK_FORMAT_ASTC_4x4_SFLOAT_BLOCK && f <= VK_FORMAT_ASTC_12x12_SFLOAT_BLOCK);
}

static void flag_names(char *out, size_t n, VkImageCreateFlags flags) {
    static const struct { VkImageCreateFlags bit; const char *name; } table[] = {
        {VK_IMAGE_CREATE_SPARSE_BINDING_BIT, "SPARSE_BINDING"},
        {VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT, "MUTABLE_FORMAT"},
        {VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT, "CUBE_COMPATIBLE"},
        {VK_IMAGE_CREATE_ALIAS_BIT, "ALIAS"},
        {VK_IMAGE_CREATE_2D_ARRAY_COMPATIBLE_BIT, "2D_ARRAY_COMPATIBLE"},
        {VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT, "BLOCK_TEXEL_VIEW_COMPATIBLE"},
        {VK_IMAGE_CREATE_EXTENDED_USAGE_BIT, "EXTENDED_USAGE"},
    };
    out[0] = 0;
    for (size_t i = 0; i < sizeof table / sizeof *table; i++)
        if (flags & table[i].bit) {
            strlcat(out, out[0] ? "|" : "", n);
            strlcat(out, table[i].name, n);
        }
    if (!out[0]) strlcpy(out, "0", n);
}

static void usage_names(char *out, size_t n, VkImageUsageFlags usage) {
    static const char *names[] = {"TRANSFER_SRC", "TRANSFER_DST", "SAMPLED", "STORAGE",
                                  "COLOR_ATTACHMENT", "DEPTH_STENCIL_ATTACHMENT", "TRANSIENT", "INPUT_ATTACHMENT"};
    out[0] = 0;
    for (int i = 0; i < 8; i++)
        if (usage & (1u << i)) {
            strlcat(out, out[0] ? "|" : "", n);
            strlcat(out, names[i], n);
        }
    if (usage >> 8) {
        char extra[32];
        snprintf(extra, sizeof extra, "%s0x%x", out[0] ? "|" : "", usage & ~0xffu);
        strlcat(out, extra, n);
    }
}

static void describe_formats(char *out, size_t n, const void *pnext) {
    out[0] = 0;
    for (const VkBaseInStructure *s = pnext; s; s = s->pNext) {
        if (s->sType == VK_STRUCTURE_TYPE_IMAGE_FORMAT_LIST_CREATE_INFO) {
            const VkImageFormatListCreateInfo *l = (const void *)s;
            strlcat(out, " view_formats=", n);
            for (uint32_t i = 0; i < l->viewFormatCount; i++) {
                char one[64];
                snprintf(one, sizeof one, "%s%s(%d)", i ? "," : "", format_name(l->pViewFormats[i]),
                         l->pViewFormats[i]);
                strlcat(out, one, n);
            }
        } else if (s->sType == VK_STRUCTURE_TYPE_IMAGE_STENCIL_USAGE_CREATE_INFO) {
            strlcat(out, " +stencil_usage", n);
        } else {
            char one[48];
            snprintf(one, sizeof one, " +sType%d", s->sType);
            strlcat(out, one, n);
        }
    }
}

/* ---- hooks ---- */

static VKAPI_ATTR VkResult VKAPI_CALL CreateImage(VkDevice device, const VkImageCreateInfo *info,
                                                  const VkAllocationCallbacks *alloc, VkImage *image) {
    device_t *d = find_device(device);
    VkImageCreateInfo copy = *info;
    int stripped = 0;
    if (strip_block_texel && (copy.flags & VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT)) {
        copy.flags &= ~VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT;
        stripped = 1;
    }
    VkResult r = d->create_image(device, &copy, alloc, image);

    const VkImageCreateFlags views = VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT |
                                     VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT;
    int interesting = r != VK_SUCCESS || (info->flags & VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT) ||
                      ((info->flags & views) && is_compressed(info->format));
    pthread_mutex_lock(&lock);
    image_count++;
    if (r == VK_SUCCESS && (info->flags & views)) {
        for (int i = 0; i < MAX_IMAGES; i++)
            if (!images[i].image) {
                images[i] = (image_t){*image, info->format, info->flags, info->usage, info->extent};
                break;
            }
    }
    if (interesting || log_all) {
        char flags[160], usage[160], extra[512];
        flag_names(flags, sizeof flags, info->flags);
        usage_names(usage, sizeof usage, info->usage);
        describe_formats(extra, sizeof extra, info->pNext);
        logged_count++;
        fprintf(stderr,
                "imagelog: vkCreateImage #%lu -> %d%s image=%p type=%dD format=%s(%d) extent=%ux%ux%u "
                "mips=%u layers=%u samples=%d tiling=%d flags=%s usage=%s%s\n",
                image_count, r, stripped ? " (BLOCK_TEXEL_VIEW_COMPATIBLE stripped)" : "",
                r == VK_SUCCESS ? (void *)*image : NULL, info->imageType + 1, format_name(info->format),
                info->format, info->extent.width, info->extent.height, info->extent.depth, info->mipLevels,
                info->arrayLayers, info->samples, info->tiling, flags, usage, extra);
    }
    pthread_mutex_unlock(&lock);
    return r;
}

static VKAPI_ATTR void VKAPI_CALL DestroyImage(VkDevice device, VkImage image, const VkAllocationCallbacks *alloc) {
    device_t *d = find_device(device);
    pthread_mutex_lock(&lock);
    for (int i = 0; image && i < MAX_IMAGES; i++)
        if (images[i].image == image) {
            images[i].image = VK_NULL_HANDLE;
            break;
        }
    pthread_mutex_unlock(&lock);
    d->destroy_image(device, image, alloc);
}

static VKAPI_ATTR VkResult VKAPI_CALL CreateImageView(VkDevice device, const VkImageViewCreateInfo *info,
                                                      const VkAllocationCallbacks *alloc, VkImageView *view) {
    device_t *d = find_device(device);
    VkResult r = d->create_image_view(device, info, alloc, view);
    pthread_mutex_lock(&lock);
    for (int i = 0; i < MAX_IMAGES; i++)
        if (images[i].image && images[i].image == info->image) {
            if (images[i].format != info->format || r != VK_SUCCESS || log_all) {
                char usage[160] = "";
                for (const VkBaseInStructure *s = info->pNext; s; s = s->pNext)
                    if (s->sType == VK_STRUCTURE_TYPE_IMAGE_VIEW_USAGE_CREATE_INFO)
                        usage_names(usage, sizeof usage, ((const VkImageViewUsageCreateInfo *)s)->usage);
                fprintf(stderr,
                        "imagelog: vkCreateImageView -> %d image=%p (%s %ux%u) view format=%s(%d) type=%d "
                        "mips=%u+%u layers=%u+%u%s%s\n",
                        r, (void *)info->image, format_name(images[i].format), images[i].extent.width,
                        images[i].extent.height, format_name(info->format), info->format, info->viewType,
                        info->subresourceRange.baseMipLevel, info->subresourceRange.levelCount,
                        info->subresourceRange.baseArrayLayer, info->subresourceRange.layerCount,
                        usage[0] ? " view_usage=" : "", usage);
            }
            break;
        }
    pthread_mutex_unlock(&lock);
    return r;
}

/* ---- instance and device plumbing ---- */

static VkLayerInstanceCreateInfo *instance_chain(const VkInstanceCreateInfo *info) {
    for (const VkBaseInStructure *s = info->pNext; s; s = s->pNext)
        if (s->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO &&
            ((const VkLayerInstanceCreateInfo *)s)->function == VK_LAYER_LINK_INFO)
            return (VkLayerInstanceCreateInfo *)s;
    return NULL;
}

static VkLayerDeviceCreateInfo *device_chain(const VkDeviceCreateInfo *info) {
    for (const VkBaseInStructure *s = info->pNext; s; s = s->pNext)
        if (s->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO &&
            ((const VkLayerDeviceCreateInfo *)s)->function == VK_LAYER_LINK_INFO)
            return (VkLayerDeviceCreateInfo *)s;
    return NULL;
}

static VKAPI_ATTR VkResult VKAPI_CALL CreateInstance(const VkInstanceCreateInfo *info,
                                                     const VkAllocationCallbacks *alloc, VkInstance *instance) {
    settings();
    VkLayerInstanceCreateInfo *chain = instance_chain(info);
    if (!chain) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = chain->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkCreateInstance create = (PFN_vkCreateInstance)gipa(VK_NULL_HANDLE, "vkCreateInstance");
    chain->u.pLayerInfo = chain->u.pLayerInfo->pNext;
    VkResult r = create(info, alloc, instance);
    if (r != VK_SUCCESS) return r;
    pthread_mutex_lock(&lock);
    for (int i = 0; i < MAX_INSTANCES; i++)
        if (!instances[i].key) {
            instances[i] = (instance_t){key_of(*instance), gipa,
                                        (PFN_vkDestroyInstance)gipa(*instance, "vkDestroyInstance")};
            break;
        }
    pthread_mutex_unlock(&lock);
    return r;
}

static VKAPI_ATTR void VKAPI_CALL DestroyInstance(VkInstance instance, const VkAllocationCallbacks *alloc) {
    instance_t *in = find_instance(instance);
    PFN_vkDestroyInstance destroy = in->destroy_instance;
    pthread_mutex_lock(&lock);
    in->key = NULL;
    pthread_mutex_unlock(&lock);
    destroy(instance, alloc);
}

static VKAPI_ATTR VkResult VKAPI_CALL CreateDevice(VkPhysicalDevice gpu, const VkDeviceCreateInfo *info,
                                                   const VkAllocationCallbacks *alloc, VkDevice *device) {
    VkLayerDeviceCreateInfo *chain = device_chain(info);
    if (!chain) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = chain->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr gdpa = chain->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    PFN_vkCreateDevice create = (PFN_vkCreateDevice)gipa(VK_NULL_HANDLE, "vkCreateDevice");
    chain->u.pLayerInfo = chain->u.pLayerInfo->pNext;
    VkResult r = create(gpu, info, alloc, device);
    if (r != VK_SUCCESS) return r;
    pthread_mutex_lock(&lock);
    for (int i = 0; i < MAX_DEVICES; i++)
        if (!devices[i].key) {
            devices[i] = (device_t){
                key_of(*device),
                gdpa,
                (PFN_vkDestroyDevice)gdpa(*device, "vkDestroyDevice"),
                (PFN_vkCreateImage)gdpa(*device, "vkCreateImage"),
                (PFN_vkDestroyImage)gdpa(*device, "vkDestroyImage"),
                (PFN_vkCreateImageView)gdpa(*device, "vkCreateImageView"),
            };
            break;
        }
    pthread_mutex_unlock(&lock);
    fprintf(stderr, "imagelog: device %p created\n", (void *)*device);
    return r;
}

static VKAPI_ATTR void VKAPI_CALL DestroyDevice(VkDevice device, const VkAllocationCallbacks *alloc) {
    device_t *d = find_device(device);
    PFN_vkDestroyDevice destroy = d->destroy_device;
    pthread_mutex_lock(&lock);
    fprintf(stderr, "imagelog: device %p destroyed after %lu images (%lu logged)\n", (void *)device, image_count,
            logged_count);
    d->key = NULL;
    pthread_mutex_unlock(&lock);
    destroy(device, alloc);
}

static PFN_vkVoidFunction device_hook(const char *name) {
#define HOOK(n) if (!strcmp(name, "vk" #n)) return (PFN_vkVoidFunction)n;
    HOOK(CreateImage) HOOK(DestroyImage) HOOK(CreateImageView) HOOK(DestroyDevice)
#undef HOOK
    return NULL;
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL imagelog_GetDeviceProcAddr(VkDevice device, const char *name) {
    PFN_vkVoidFunction f = device_hook(name);
    device_t *d = find_device(device);
    return f ? f : d ? d->gdpa(device, name) : NULL;
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL imagelog_GetInstanceProcAddr(VkInstance instance, const char *name) {
    if (!strcmp(name, "vkGetInstanceProcAddr")) return (PFN_vkVoidFunction)imagelog_GetInstanceProcAddr;
    if (!strcmp(name, "vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)imagelog_GetDeviceProcAddr;
    if (!strcmp(name, "vkCreateInstance")) return (PFN_vkVoidFunction)CreateInstance;
    if (!strcmp(name, "vkDestroyInstance")) return (PFN_vkVoidFunction)DestroyInstance;
    if (!strcmp(name, "vkCreateDevice")) return (PFN_vkVoidFunction)CreateDevice;
    PFN_vkVoidFunction f = device_hook(name);
    if (f) return f;
    instance_t *in = instance ? find_instance(instance) : NULL;
    return in ? in->gipa(instance, name) : NULL;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL imagelog_NegotiateLoaderLayerInterfaceVersion(
    VkNegotiateLayerInterface *v) {
    if (v->loaderLayerInterfaceVersion > 2) v->loaderLayerInterfaceVersion = 2;
    v->pfnGetInstanceProcAddr = imagelog_GetInstanceProcAddr;
    v->pfnGetDeviceProcAddr = imagelog_GetDeviceProcAddr;
    v->pfnGetPhysicalDeviceProcAddr = NULL;
    return VK_SUCCESS;
}

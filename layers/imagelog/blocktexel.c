/*
 * blocktexel: what Unreal's Vulkan renderer asks MoltenVK for when it writes
 * block-compressed textures from a compute shader. Exit status 0 when all
 * steps work.
 *
 *   1. a BC image created with BLOCK_TEXEL_VIEW_COMPATIBLE | MUTABLE_FORMAT |
 *      EXTENDED_USAGE (MoltenVK 1.4.0 refuses: VK_ERROR_FEATURE_NOT_PRESENT);
 *   2. a sampled view of it in its own BC format;
 *   3. an uncompressed (R32G32B32A32_UINT) storage view of it. Metal has no
 *      such texture view (bcview.m); MoltenVK 1.4.1 and later make it as a
 *      second texture on the same heap memory.
 *
 * Built and run by layers/imagelog/build.sh --test (x86_64, the loader and
 * MoltenVK bundled with the wine11 runtime).
 */

#include <stdio.h>
#include <string.h>

#include <vulkan/vulkan.h>

#define CHECK(what, r) printf("%-48s %s (%d)\n", what, (r) == VK_SUCCESS ? "ok" : "FAILED", (r))

int main(void) {
    const char *exts[] = {VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME};
    VkApplicationInfo app = {VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
                                .flags = VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR,
                                .pApplicationInfo = &app, .enabledExtensionCount = 1, .ppEnabledExtensionNames = exts};
    VkInstance instance;
    VkResult r = vkCreateInstance(&ici, NULL, &instance);
    CHECK("vkCreateInstance", r);
    if (r) return 2;
    uint32_t n = 1;
    VkPhysicalDevice gpu;
    vkEnumeratePhysicalDevices(instance, &n, &gpu);
    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(gpu, &props);
    printf("device: %s, driver 0x%x\n", props.deviceName, props.driverVersion);

    float prio = 1;
    VkDeviceQueueCreateInfo qci = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueCount = 1, .pQueuePriorities = &prio};
    const char *dexts[] = {"VK_KHR_portability_subset"};
    VkDeviceCreateInfo dci = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1,
                              .pQueueCreateInfos = &qci, .enabledExtensionCount = 1, .ppEnabledExtensionNames = dexts};
    VkDevice device;
    r = vkCreateDevice(gpu, &dci, NULL, &device);
    CHECK("vkCreateDevice", r);
    if (r) return 2;

    int failures = 0;
    VkFormat bc = VK_FORMAT_BC3_UNORM_BLOCK;
    VkImageCreateInfo ii = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
                            .flags = VK_IMAGE_CREATE_BLOCK_TEXEL_VIEW_COMPATIBLE_BIT |
                                     VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT | VK_IMAGE_CREATE_EXTENDED_USAGE_BIT,
                            .imageType = VK_IMAGE_TYPE_2D, .format = bc, .extent = {256, 256, 1},
                            .mipLevels = 1, .arrayLayers = 1, .samples = VK_SAMPLE_COUNT_1_BIT,
                            .tiling = VK_IMAGE_TILING_OPTIMAL,
                            .usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT |
                                     VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT};
    VkImage image;
    r = vkCreateImage(device, &ii, NULL, &image);
    CHECK("1. BC3 image, BLOCK_TEXEL_VIEW_COMPATIBLE", r);
    if (r) return 1;

    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(device, image, &req);
    VkMemoryAllocateInfo mai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size};
    VkPhysicalDeviceMemoryProperties mem;
    vkGetPhysicalDeviceMemoryProperties(gpu, &mem);
    for (uint32_t i = 0; i < mem.memoryTypeCount; i++)
        if (req.memoryTypeBits & (1u << i)) { mai.memoryTypeIndex = i; break; }
    VkDeviceMemory memory;
    vkAllocateMemory(device, &mai, NULL, &memory);
    vkBindImageMemory(device, image, memory, 0);

    VkImageViewUsageCreateInfo sampled = {VK_STRUCTURE_TYPE_IMAGE_VIEW_USAGE_CREATE_INFO,
                                          .usage = VK_IMAGE_USAGE_SAMPLED_BIT};
    VkImageViewCreateInfo vi = {VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .pNext = &sampled, .image = image,
                                .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = bc,
                                .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    VkImageView view;
    r = vkCreateImageView(device, &vi, NULL, &view);
    CHECK("2. sampled BC3 view", r);
    failures += r != VK_SUCCESS;
    if (!r) vkDestroyImageView(device, view, NULL);

    VkImageViewUsageCreateInfo storage = {VK_STRUCTURE_TYPE_IMAGE_VIEW_USAGE_CREATE_INFO,
                                          .usage = VK_IMAGE_USAGE_STORAGE_BIT};
    vi.pNext = &storage;
    vi.format = VK_FORMAT_R32G32B32A32_UINT;
    r = vkCreateImageView(device, &vi, NULL, &view);
    CHECK("3. uncompressed R32G32B32A32_UINT storage view", r);
    failures += r != VK_SUCCESS;
    if (!r) vkDestroyImageView(device, view, NULL);

    vkDestroyImage(device, image, NULL);
    vkFreeMemory(device, memory, NULL);
    vkDestroyDevice(device, NULL);
    vkDestroyInstance(instance, NULL);
    return failures;
}

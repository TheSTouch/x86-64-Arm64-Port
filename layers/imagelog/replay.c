/*
 * replay: rebuilds a pipeline that failed in a game, from the record the
 * imagelog layer wrote (IMAGELOG_DUMP), and asks the driver for it again.
 * Exit status 0 when every record given now succeeds.
 *
 *   replay <dump>/pipeline-0001-compute.txt [more records...]
 *
 * Compute pipelines are rebuilt whole. Graphics records are listed with their
 * stages but not rebuilt (their fixed-function state is not recorded).
 * Shader modules are read from the record's folder (<hash>.spv).
 */

#include <libgen.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <vulkan/vulkan.h>

#define MAX_SETS 16
#define MAX_BINDINGS 256

static VkDevice device;
static VkSampler sampler;

static void *read_file(const char *path, size_t *size) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    *size = ftell(f);
    fseek(f, 0, SEEK_SET);
    void *data = malloc(*size);
    fread(data, 1, *size, f);
    fclose(f);
    return data;
}

static int replay(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) {
        perror(path);
        return 1;
    }
    char dir[1024];
    strlcpy(dir, path, sizeof dir);
    strlcpy(dir, dirname(dir), sizeof dir);

    char kind[32] = "", line[4096], entry[256] = "main";
    int recorded = 0;
    uint32_t set_count = 0, push_count = 0, spec_count = 0;
    VkPushConstantRange push[16];
    VkDescriptorSetLayoutCreateFlags set_flags[MAX_SETS] = {0};
    uint32_t binding_count[MAX_SETS] = {0};
    static VkDescriptorSetLayoutBinding bindings[MAX_SETS][MAX_BINDINGS];
    static VkDescriptorBindingFlags binding_flags[MAX_SETS][MAX_BINDINGS];
    static VkSampler immutable[MAX_BINDINGS * 64];
    for (size_t i = 0; i < sizeof immutable / sizeof *immutable; i++) immutable[i] = sampler;
    VkSpecializationMapEntry spec[256];
    uint8_t spec_data[4096];
    size_t spec_size = 0;
    unsigned long long module_hash = 0;
    unsigned stage = 0;
    int cur = -1;

    while (fgets(line, sizeof line, f)) {
        unsigned a, b, c, d;
        int t, imm;
        char hex[8192];
        if (sscanf(line, "pipeline %31s result %d", kind, &recorded) == 2) continue;
        if (sscanf(line, "push %u %u %u", &a, &b, &c) == 3 && push_count < 16) {
            push[push_count++] = (VkPushConstantRange){a, b, c};
        } else if (sscanf(line, "set %u flags 0x%x bindings %u", &a, &b, &c) == 3 && a < MAX_SETS) {
            cur = a;
            set_flags[a] = b;
            set_count = a + 1;
        } else if (sscanf(line, "binding %u type %d count %u stages 0x%x flags 0x%x immutable %d", &a, &t, &b, &c,
                          &d, &imm) == 6 && cur >= 0 && binding_count[cur] < MAX_BINDINGS) {
            uint32_t n = binding_count[cur]++;
            bindings[cur][n] = (VkDescriptorSetLayoutBinding){a, t, b, c, imm ? immutable : NULL};
            binding_flags[cur][n] = d;
        } else if (sscanf(line, "stage 0x%x module %llx entry %255s", &stage, &module_hash, entry) == 3) {
        } else if (sscanf(line, "spec %u %u %u", &a, &b, &c) == 3 && spec_count < 256) {
            spec[spec_count++] = (VkSpecializationMapEntry){a, b, c};
        } else if (sscanf(line, "specdata %8191s", hex) == 1) {
            for (spec_size = 0; hex[spec_size * 2] && spec_size < sizeof spec_data; spec_size++)
                sscanf(hex + spec_size * 2, "%2hhx", &spec_data[spec_size]);
        }
    }
    fclose(f);

    if (strcmp(kind, "compute")) {
        printf("%s: %s pipeline, recorded result %d: not replayed\n", path, kind, recorded);
        return 0;
    }

    VkDescriptorSetLayout set_layouts[MAX_SETS];
    for (uint32_t s = 0; s < set_count; s++) {
        int any_flags = 0;
        for (uint32_t i = 0; i < binding_count[s]; i++) any_flags |= binding_flags[s][i] != 0;
        VkDescriptorSetLayoutBindingFlagsCreateInfo bf = {
            VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_BINDING_FLAGS_CREATE_INFO, NULL, binding_count[s], binding_flags[s]};
        VkDescriptorSetLayoutCreateInfo ci = {VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                              any_flags ? &bf : NULL, set_flags[s], binding_count[s], bindings[s]};
        if (vkCreateDescriptorSetLayout(device, &ci, NULL, &set_layouts[s])) {
            printf("%s: set layout %u FAILED\n", path, s);
            return 1;
        }
    }
    VkPipelineLayoutCreateInfo pl = {VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, NULL, 0, set_count, set_layouts,
                                     push_count, push};
    VkPipelineLayout layout;
    vkCreatePipelineLayout(device, &pl, NULL, &layout);

    char spv[1200];
    snprintf(spv, sizeof spv, "%s/%016llx.spv", dir, module_hash);
    size_t size;
    uint32_t *code = read_file(spv, &size);
    if (!code) {
        printf("%s: %s missing\n", path, spv);
        return 1;
    }
    VkShaderModuleCreateInfo mi = {VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, NULL, 0, size, code};
    VkShaderModule module;
    vkCreateShaderModule(device, &mi, NULL, &module);
    VkSpecializationInfo si = {spec_count, spec, spec_size, spec_data};
    VkComputePipelineCreateInfo ci = {
        VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
        .stage = {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, NULL, 0, VK_SHADER_STAGE_COMPUTE_BIT, module, entry,
                  spec_count || spec_size ? &si : NULL},
        .layout = layout};
    VkPipeline pipeline;
    VkResult r = vkCreateComputePipelines(device, VK_NULL_HANDLE, 1, &ci, NULL, &pipeline);
    printf("%s: compute %016llx: %s (%d; in the game %d)\n", path, module_hash, r ? "FAILED" : "ok", r, recorded);
    if (!r) vkDestroyPipeline(device, pipeline, NULL);
    vkDestroyShaderModule(device, module, NULL);
    vkDestroyPipelineLayout(device, layout, NULL);
    for (uint32_t s = 0; s < set_count; s++) vkDestroyDescriptorSetLayout(device, set_layouts[s], NULL);
    free(code);
    return r != VK_SUCCESS;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: replay <pipeline record>...\n");
        return 2;
    }
#ifdef DEFAULT_ICD
    setenv("VK_DRIVER_FILES", DEFAULT_ICD, 0); /* the runtime's MoltenVK unless told otherwise */
#endif
    const char *exts[] = {VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME,
                          VK_KHR_GET_PHYSICAL_DEVICE_PROPERTIES_2_EXTENSION_NAME};
    VkApplicationInfo app = {VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
                                .flags = VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR,
                                .pApplicationInfo = &app, .enabledExtensionCount = 2, .ppEnabledExtensionNames = exts};
    VkInstance instance;
    VkResult r;
    if ((r = vkCreateInstance(&ici, NULL, &instance))) {
        fprintf(stderr, "vkCreateInstance failed (%d)\n", r);
        return 2;
    }
    uint32_t n = 1;
    VkPhysicalDevice gpu;
    vkEnumeratePhysicalDevices(instance, &n, &gpu);

    /* The extensions Unreal enabled in the game, and every feature they offer. */
    const char *dexts[] = {"VK_KHR_portability_subset", "VK_KHR_push_descriptor", "VK_EXT_descriptor_indexing",
                           "VK_KHR_maintenance4", "VK_KHR_maintenance5", "VK_KHR_16bit_storage",
                           "VK_KHR_shader_float16_int8", "VK_EXT_scalar_block_layout",
                           "VK_EXT_subgroup_size_control", "VK_KHR_spirv_1_4", "VK_KHR_shader_float_controls",
                           "VK_EXT_shader_demote_to_helper_invocation", "VK_KHR_buffer_device_address",
                           "VK_KHR_synchronization2", "VK_KHR_dynamic_rendering", "VK_KHR_timeline_semaphore",
                           "VK_KHR_image_format_list", "VK_EXT_shader_viewport_index_layer",
                           "VK_KHR_fragment_shader_barycentric"};
    VkPhysicalDeviceDescriptorIndexingFeatures indexing = {
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES};
    VkPhysicalDeviceShaderFloat16Int8Features f16 = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SHADER_FLOAT16_INT8_FEATURES,
                                                     &indexing};
    VkPhysicalDevice16BitStorageFeatures s16 = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_16BIT_STORAGE_FEATURES, &f16};
    VkPhysicalDeviceFeatures2 features = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, &s16};
    vkGetPhysicalDeviceFeatures2(gpu, &features);
    float prio = 1;
    VkDeviceQueueCreateInfo qci = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueCount = 1, .pQueuePriorities = &prio};
    VkDeviceCreateInfo dci = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, &features, .queueCreateInfoCount = 1,
                              .pQueueCreateInfos = &qci, .enabledExtensionCount = sizeof dexts / sizeof *dexts,
                              .ppEnabledExtensionNames = dexts};
    r = vkCreateDevice(gpu, &dci, NULL, &device);
    if (r) {
        fprintf(stderr, "vkCreateDevice failed (%d)\n", r);
        return 2;
    }
    VkSamplerCreateInfo sci = {VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO, .maxLod = 16};
    vkCreateSampler(device, &sci, NULL, &sampler);

    int failures = 0;
    for (int i = 1; i < argc; i++) failures += replay(argv[i]);
    printf("%d of %d records still fail\n", failures, argc - 1);
    vkDestroySampler(device, sampler, NULL);
    vkDestroyDevice(device, NULL);
    vkDestroyInstance(instance, NULL);
    return failures != 0;
}

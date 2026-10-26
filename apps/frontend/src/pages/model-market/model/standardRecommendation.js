import { recommendationBudget } from '../../evaluation/model/experimentDesign.js';

const GiB = 1024 ** 3;

export function standardRecommendation(model, hardware, resourceBasis = 'available') {
    const availableGpus = recommendationBudget(hardware, resourceBasis);
    const totalVramGiB = (hardware?.vramBytes || 0) / GiB;
    const vramPerGpuGiB = availableGpus > 0 ? totalVramGiB / availableGpus : 0;
    const requiredVramGiB = Number.isFinite(model.sizeGiB) && model.sizeGiB > 0 ? model.sizeGiB * 1.2 : 0;
    const tensorParallelism = vramPerGpuGiB > 0 ? Math.max(1, Math.ceil(requiredVramGiB / vramPerGpuGiB)) : 1;
    const replicas = availableGpus >= tensorParallelism
        ? Math.min(4, Math.floor(availableGpus / tensorParallelism))
        : 1;
    return {
        tensorParallelism,
        replicas,
        maxModelLen: model.contextLength ?? 4096,
        cpuOffload: requiredVramGiB > totalVramGiB && totalVramGiB > 0,
        availableGpus,
        vramPerGpuGiB,
    };
}

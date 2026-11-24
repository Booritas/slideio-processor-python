// Stages: _PipelineSpec (the library's spec, through its JSON form) and the stage registry
// of a _Workspace by name. Every call that reaches the workspace runs with the GIL released;
// the Python arguments are converted before and the answers built after.
#include "bindings.hpp"

#include <pybind11/stl.h>

#include <slideio/processor/core/error.hpp>
#include <slideio/processor/core/flow.hpp>
#include <slideio/processor/core/ids.hpp>
#include <slideio/processor/pipeline/pipeline_spec.hpp>
#include <slideio/processor/storage/layer_registry.hpp>
#include <slideio/processor/storage/raster_store.hpp>
#include <slideio/processor/storage/store.hpp>
#include <slideio/processor/storage/types.hpp>
#include <slideio/processor/workspace/stage_definition.hpp>
#include <slideio/processor/workspace/stage_registry.hpp>
#include <slideio/processor/workspace/workspace.hpp>

#include <cstdint>
#include <optional>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

namespace sp = slideio::processor;

[[noreturn]] void refuse(const std::string& text) {
    throw sp::InvalidArgumentError(("Workspace: " + text).c_str());
}

std::string specJson(const sp::PipelineSpec& spec) {
    const int32_t length = spec.toJson(nullptr, 0);
    std::string text(static_cast<size_t>(length) + 1, '\0');
    spec.toJson(text.data(), length + 1);
    text.resize(static_cast<size_t>(length));
    return text;
}

sp::LayerId layerOf(sp::Workspace& workspace, const std::string& name) {
    sp::LayerId id;
    if (!workspace.layers().find(name.c_str(), id)) refuse("no layer named '" + name + "'");
    return id;
}

sp::RasterId rasterOf(sp::Workspace& workspace, const std::string& name) {
    sp::RasterId id;
    if (!workspace.store()->rasters().find(name.c_str(), id)) refuse("no raster named '" + name + "'");
    return id;
}

sp::StageId stageOf(sp::StageRegistry& stages, const std::string& name) {
    sp::StageId id;
    if (!stages.find(name.c_str(), id)) refuse("no stage named '" + name + "'");
    return id;
}

// A stage definition by names, as Python passes it.
struct DefinitionNames {
    std::string name;
    std::vector<std::string> readsLayers;
    std::vector<std::string> writesLayers;
    std::vector<std::string> annotatesLayers;
    std::vector<std::string> dependsOn;
    std::optional<std::string> writesRaster;
    std::vector<std::pair<std::string, std::string>> readsRasters;
    std::vector<std::tuple<std::string, std::string, int32_t>> readsLayersAsRaster;
    std::optional<std::string> restrictToLayer;
};

sp::LayerRasterContent contentOf(int32_t value) {
    if (value < static_cast<int32_t>(sp::LayerRasterContent::Label) ||
        value > static_cast<int32_t>(sp::LayerRasterContent::ObjectAnchor)) {
        refuse("the layer raster content " + std::to_string(value) + " is not a LayerRasterContent");
    }
    return static_cast<sp::LayerRasterContent>(value);
}

// Resolves every name and declares the stage; returns its name. Call with the GIL released.
std::string declareStage(sp::Workspace& workspace, const DefinitionNames& names, const sp::PipelineSpec& spec) {
    sp::StageDefinition definition(names.name.c_str());
    definition.pipeline(spec);
    for (const std::string& layer : names.readsLayers) definition.readsLayer(layerOf(workspace, layer));
    for (const std::string& layer : names.writesLayers) definition.writesLayer(layerOf(workspace, layer));
    for (const std::string& layer : names.annotatesLayers) definition.annotatesLayer(layerOf(workspace, layer));
    for (const std::string& stage : names.dependsOn) definition.dependsOn(stage.c_str());
    if (names.writesRaster) definition.writesRaster(rasterOf(workspace, *names.writesRaster));
    for (const auto& [binding, raster] : names.readsRasters) {
        definition.readsRaster(binding.c_str(), rasterOf(workspace, raster));
    }
    for (const auto& [binding, layer, content] : names.readsLayersAsRaster) {
        definition.readsLayerAsRaster(binding.c_str(), layerOf(workspace, layer), contentOf(content));
    }
    if (names.restrictToLayer) definition.restrictToLayer(layerOf(workspace, *names.restrictToLayer));
    sp::StageRegistry& stages = workspace.stages();
    return stages.name(stages.declare(definition));
}

struct StageRow {
    std::string name;
    int32_t status;
    bool stale;
    bool deterministic;
    int64_t completedAtUnixMs;
    std::vector<std::string> dependencies;
    std::vector<std::string> unresolved;
    bool runnable;
    int32_t blocker;
    std::optional<std::string> blockerName;
    std::string record;
};

// Reads what the registry reports about a stage. Call with the GIL released.
StageRow stageRow(sp::Workspace& workspace, const std::string& name) {
    sp::StageRegistry& stages = workspace.stages();
    const sp::StageId id = stageOf(stages, name);
    StageRow row;
    row.name = stages.name(id);
    row.status = static_cast<int32_t>(stages.status(id));
    row.stale = stages.isStale(id);
    row.deterministic = stages.isDeterministic(id);
    row.completedAtUnixMs = stages.completedAtUnixMs(id);
    const int32_t dependencyCount = stages.dependencyCount(id);
    for (int32_t i = 0; i < dependencyCount; ++i) {
        row.dependencies.emplace_back(stages.name(stages.dependencyAt(id, i)));
    }
    const int32_t unresolvedCount = stages.unresolvedDependencyCount(id);
    for (int32_t i = 0; i < unresolvedCount; ++i) row.unresolved.emplace_back(stages.unresolvedDependencyAt(id, i));
    // runBlocker reads in one hold of the registry mutex; runnable is derived from it, as
    // isRunnable() is exactly "runBlocker() == None", so the two never disagree.
    const char* blockerName = nullptr;
    const sp::RunBlocker blocker = stages.runBlocker(id, blockerName);
    row.blocker = static_cast<int32_t>(blocker);
    row.runnable = blocker == sp::RunBlocker::None;
    if (blockerName != nullptr) row.blockerName = std::string(blockerName);
    row.record = stages.configuration(id);
    return row;
}

std::vector<std::string> stageNames(sp::Workspace& workspace) {
    sp::StageRegistry& stages = workspace.stages();
    std::vector<std::string> names;
    const int32_t count = stages.count();
    for (int32_t i = 0; i < count; ++i) names.emplace_back(stages.name(stages.at(i)));
    return names;
}

void markStale(sp::Workspace& workspace, const std::string& name, bool downstream) {
    sp::StageRegistry& stages = workspace.stages();
    // The library refuses a read-only workspace before resolving the stage; do the same.
    if (workspace.isReadOnly()) {
        throw sp::IoError("Workspace: the workspace is read-only");
    }
    const sp::StageId id = stageOf(stages, name);
    if (downstream) {
        stages.markAffectedStale(id);
    } else {
        stages.markStale(id);
    }
}

} // namespace

void bindStages(py::module_& m) {
    using Release = py::call_guard<py::gil_scoped_release>;

    // A spec holds no workspace and takes no lock: its calls keep the GIL.
    py::class_<sp::PipelineSpec>(m, "_PipelineSpec")
        .def_static("from_json", [](const std::string& text) { return sp::PipelineSpec::fromJson(text.c_str()); },
                    py::arg("text"))
        .def("to_json", &specJson)
        .def("equals", &sp::PipelineSpec::equals, py::arg("other"));

    m.def(
        "_declare_stage",
        [](sp::Workspace& workspace, const std::string& name, const sp::PipelineSpec& spec,
           std::vector<std::string> readsLayers, std::vector<std::string> writesLayers,
           std::vector<std::string> annotatesLayers, std::vector<std::string> dependsOn,
           std::optional<std::string> writesRaster, std::vector<std::pair<std::string, std::string>> readsRasters,
           std::vector<std::tuple<std::string, std::string, int32_t>> readsLayersAsRaster,
           std::optional<std::string> restrictToLayer) {
            const DefinitionNames names{name,
                                        std::move(readsLayers),
                                        std::move(writesLayers),
                                        std::move(annotatesLayers),
                                        std::move(dependsOn),
                                        std::move(writesRaster),
                                        std::move(readsRasters),
                                        std::move(readsLayersAsRaster),
                                        std::move(restrictToLayer)};
            return declareStage(workspace, names, spec);
        },
        py::arg("workspace"), py::arg("name"), py::arg("spec"), py::arg("reads_layers"), py::arg("writes_layers"),
        py::arg("annotates_layers"), py::arg("depends_on"), py::arg("writes_raster"), py::arg("reads_rasters"),
        py::arg("reads_layers_as_raster"), py::arg("restrict_to_layer"), Release());

    m.def("_stage_names", &stageNames, py::arg("workspace"), Release());

    m.def(
        "_stage_info",
        [](sp::Workspace& workspace, const std::string& name) {
            StageRow row;
            {
                py::gil_scoped_release release;
                row = stageRow(workspace, name);
            }
            return py::make_tuple(row.name, row.status, row.stale, row.deterministic, row.completedAtUnixMs,
                                  py::tuple(py::cast(row.dependencies)), py::tuple(py::cast(row.unresolved)),
                                  row.runnable, row.blocker,
                                  row.blockerName ? py::object(py::str(*row.blockerName)) : py::object(py::none()),
                                  row.record);
        },
        py::arg("workspace"), py::arg("name"));

    m.def("_mark_stale", &markStale, py::arg("workspace"), py::arg("name"), py::arg("downstream"), Release());
}

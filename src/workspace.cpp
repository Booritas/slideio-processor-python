// Workspace: _Workspace (the library's Workspace, shared), its five ways to open, and its
// layers and rasters by name. Every call that reaches the library runs with the GIL released;
// Python objects are read before and built after.
#include "bindings.hpp"
#include "python_runtime.hpp"

#include <pybind11/stl.h>

#include <slideio/processor/config/configuration.hpp>
#include <slideio/processor/core/data_type.hpp>
#include <slideio/processor/core/error.hpp>
#include <slideio/processor/core/ids.hpp>
#include <slideio/processor/storage/layer_registry.hpp>
#include <slideio/processor/storage/object_store.hpp>
#include <slideio/processor/storage/raster_store.hpp>
#include <slideio/processor/storage/store.hpp>
#include <slideio/processor/workspace/layer_lifecycle.hpp>
#include <slideio/processor/workspace/workspace.hpp>

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace slideio {
class Scene;
} // namespace slideio

namespace {

namespace sp = slideio::processor;
using slideio_processor_python::numpyName;
using ScenePointer = std::shared_ptr<slideio::Scene>;

constexpr const char* kCapsuleName = "slideio.Scene";

[[noreturn]] void refuse(const std::string& text) {
    throw sp::InvalidArgumentError(("Workspace: " + text).c_str());
}

// The scene a slideio.Scene._native_handle() capsule carries. The capsule owns a heap
// std::shared_ptr<slideio::Scene> and frees it itself: the pointer is copied, never moved
// or freed. Call with the GIL held.
ScenePointer sceneOf(const py::object& capsule) {
    if (!PyCapsule_IsValid(capsule.ptr(), kCapsuleName)) refuse("the slide must be a path or a slideio.Scene");
    auto* held = static_cast<ScenePointer*>(PyCapsule_GetPointer(capsule.ptr(), kCapsuleName));
    if (held == nullptr || !*held) refuse("the slide must be a path or a slideio.Scene");
    return *held;
}

sp::LayerId findLayer(sp::LayerRegistry& layers, const std::string& name) {
    sp::LayerId id;
    if (!layers.find(name.c_str(), id)) refuse("no layer is named '" + name + "'");
    return id;
}

struct LayerRow {
    std::string name;
    int32_t kind;
    std::optional<std::string> parent;
    bool dropped;
    int64_t objects;
};

struct RasterRow {
    std::string name;
    sp::DataType type;
    int32_t planes;
    bool populated;
};

std::vector<LayerRow> layerRows(sp::Workspace& workspace) {
    sp::LayerRegistry& layers = workspace.layers();
    sp::ObjectStore& objects = workspace.store()->objects();
    const sp::LayerId pixel = layers.pixelLayer();
    std::vector<LayerRow> rows;
    const int32_t count = layers.count();
    for (int32_t i = 0; i < count; ++i) {
        const sp::LayerId id = layers.at(i);
        if (id == pixel) continue;
        LayerRow row{layers.name(id), static_cast<int32_t>(layers.kind(id)), std::nullopt, layers.isDropped(id),
                     objects.objectCountInLayer(id)};
        if (layers.kind(id) == sp::LayerKind::Semantic) {
            const sp::LayerId parent = layers.parent(id);
            if (parent.isValid() && parent != pixel) row.parent = std::string(layers.name(parent));
        }
        rows.push_back(std::move(row));
    }
    return rows;
}

// Declares a layer, or accepts one that exists with the same kind and parent, as SlideRun
// does for a pipeline file's layers. parent is empty for the pixel layer.
void declareLayer(sp::Workspace& workspace, const std::string& name, sp::LayerKind kind,
                  const std::optional<std::string>& parentName) {
    sp::LayerRegistry& layers = workspace.layers();
    sp::LayerId parent = layers.pixelLayer();
    if (parentName && !layers.find(parentName->c_str(), parent)) {
        refuse("the parent '" + *parentName + "' of layer '" + name + "' is not declared");
    }
    sp::LayerId existing;
    if (layers.find(name.c_str(), existing)) {
        const bool same = layers.kind(existing) == kind &&
                          (kind == sp::LayerKind::Primitive || layers.parent(existing) == parent);
        if (!same) refuse("the workspace has a layer '" + name + "' of another kind or parent");
        return;
    }
    if (kind == sp::LayerKind::Semantic) {
        workspace.layerLifecycle().declareSemantic(name.c_str(), parent);
    } else {
        workspace.layerLifecycle().declarePrimitive(name.c_str());
    }
}

void dropLayer(sp::Workspace& workspace, const std::string& name) {
    sp::LayerRegistry& layers = workspace.layers();
    const sp::LayerId id = findLayer(layers, name);
    if (layers.kind(id) != sp::LayerKind::Primitive) {
        refuse("layer '" + name + "' is semantic; only a primitive layer can be dropped");
    }
    workspace.layerLifecycle().drop(id);
}

sp::DataType dataTypeOf(const std::string& numpy) {
    for (int32_t i = static_cast<int32_t>(sp::DataType::Byte); i <= static_cast<int32_t>(sp::DataType::Float64);
         ++i) {
        const auto type = static_cast<sp::DataType>(i);
        if (numpy == numpyName(type)) return type;
    }
    refuse("the dtype '" + numpy + "' is not a raster sample type");
}

std::vector<RasterRow> rasterRows(sp::Workspace& workspace) {
    sp::RasterRegistry& rasters = workspace.store()->rasters();
    std::vector<RasterRow> rows;
    const int32_t count = rasters.count();
    for (int32_t i = 0; i < count; ++i) {
        const sp::RasterId id = rasters.at(i);
        rows.push_back(
            RasterRow{rasters.name(id), rasters.planeType(id), rasters.planeCount(id), rasters.isPopulated(id)});
    }
    return rows;
}

// Declares a raster, or accepts one that exists with the same type and plane count, as
// SlideRun does.
void declareRaster(sp::Workspace& workspace, const std::string& name, sp::DataType type, int32_t planes) {
    sp::RasterRegistry& rasters = workspace.store()->rasters();
    sp::RasterId existing;
    if (rasters.find(name.c_str(), existing)) {
        if (rasters.planeType(existing) != type || rasters.planeCount(existing) != planes) {
            refuse("the workspace has a raster '" + name + "' of another type or plane count");
        }
        return;
    }
    rasters.declare(sp::RasterDeclaration(name.c_str(), type, planes));
}

} // namespace

void bindWorkspace(py::module_& m) {
    using Release = py::call_guard<py::gil_scoped_release>;

    py::class_<sp::Workspace, std::shared_ptr<sp::Workspace>>(m, "_Workspace")
        .def_static(
            "create_from_path",
            [](const std::string& path, const std::string& slidePath, int32_t scene,
               const sp::Configuration& configuration) {
                return sp::Workspace::create(path.c_str(), slidePath.c_str(), scene, configuration);
            },
            py::arg("path"), py::arg("slide_path"), py::arg("scene"), py::arg("configuration"), Release())
        .def_static(
            "create_from_scene",
            [](const std::string& path, const py::object& capsule, const std::string& slidePath, int32_t scene,
               const sp::Configuration& configuration) {
                ScenePointer pointer = sceneOf(capsule);
                py::gil_scoped_release release;
                return sp::Workspace::create(path.c_str(), std::move(pointer), slidePath.c_str(), scene,
                                             configuration);
            },
            py::arg("path"), py::arg("capsule"), py::arg("slide_path"), py::arg("scene"), py::arg("configuration"))
        .def_static(
            "open",
            [](const std::string& path, const sp::Configuration& configuration) {
                return sp::Workspace::open(path.c_str(), configuration);
            },
            py::arg("path"), py::arg("configuration"), Release())
        .def_static(
            "open_with_scene",
            [](const std::string& path, const py::object& capsule, int32_t scene,
               const sp::Configuration& configuration) {
                ScenePointer pointer = sceneOf(capsule);
                py::gil_scoped_release release;
                return sp::Workspace::open(path.c_str(), std::move(pointer), scene, configuration);
            },
            py::arg("path"), py::arg("capsule"), py::arg("scene"), py::arg("configuration"))
        .def_static(
            "open_read_only",
            [](const std::string& path, const sp::Configuration& configuration) {
                return sp::Workspace::openReadOnly(path.c_str(), configuration);
            },
            py::arg("path"), py::arg("configuration"), Release())
        .def("path", [](sp::Workspace& self) { return std::string(self.path()); }, Release())
        .def("slide_path", [](sp::Workspace& self) { return std::string(self.slidePath()); }, Release())
        .def("scene_index", &sp::Workspace::sceneIndex, Release())
        .def("is_read_only", &sp::Workspace::isReadOnly, Release())
        .def("close", &sp::Workspace::close, Release())
        .def("layers",
             [](sp::Workspace& self) {
                 std::vector<LayerRow> rows;
                 {
                     py::gil_scoped_release release;
                     rows = layerRows(self);
                 }
                 py::list out;
                 for (const LayerRow& row : rows) {
                     out.append(py::make_tuple(row.name, row.kind,
                                               row.parent ? py::object(py::str(*row.parent)) : py::object(py::none()),
                                               row.dropped, row.objects));
                 }
                 return out;
             })
        .def(
            "declare_layer",
            [](sp::Workspace& self, const std::string& name, const std::optional<std::string>& parent) {
                declareLayer(self, name, sp::LayerKind::Semantic, parent);
            },
            py::arg("name"), py::arg("parent"), Release())
        .def(
            "declare_primitive_layer",
            [](sp::Workspace& self, const std::string& name) {
                declareLayer(self, name, sp::LayerKind::Primitive, std::nullopt);
            },
            py::arg("name"), Release())
        .def("drop_layer", &dropLayer, py::arg("name"), Release())
        .def("rasters",
             [](sp::Workspace& self) {
                 std::vector<RasterRow> rows;
                 {
                     py::gil_scoped_release release;
                     rows = rasterRows(self);
                 }
                 py::list out;
                 for (const RasterRow& row : rows) {
                     out.append(py::make_tuple(row.name, std::string(numpyName(row.type)), row.planes, row.populated));
                 }
                 return out;
             })
        .def(
            "declare_raster",
            [](sp::Workspace& self, const std::string& name, const std::string& dtype, int32_t planes) {
                declareRaster(self, name, dataTypeOf(dtype), planes);
            },
            py::arg("name"), py::arg("dtype"), py::arg("planes"), Release());
}

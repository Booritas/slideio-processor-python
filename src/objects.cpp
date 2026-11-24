// Objects: queries of a _Workspace's object store by name, a _Cursor that fetches records in
// batches, counts, point reads and the store's measurement names. Every call that reaches the
// store runs with the GIL released, name resolution included; the Python arguments are read
// before and the Python answers built after.
#include "bindings.hpp"

#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include <slideio/processor/core/error.hpp>
#include <slideio/processor/core/flow.hpp>
#include <slideio/processor/core/geometry.hpp>
#include <slideio/processor/core/ids.hpp>
#include <slideio/processor/core/measurement.hpp>
#include <slideio/processor/core/object_query.hpp>
#include <slideio/processor/core/object_record.hpp>
#include <slideio/processor/storage/layer_registry.hpp>
#include <slideio/processor/storage/object_store.hpp>
#include <slideio/processor/storage/store.hpp>
#include <slideio/processor/storage/vocabulary_registry.hpp>
#include <slideio/processor/workspace/stage_registry.hpp>
#include <slideio/processor/workspace/workspace.hpp>

#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

namespace sp = slideio::processor;
using WorkspaceHolder = std::shared_ptr<sp::Workspace>;

[[noreturn]] void refuse(const std::string& text) {
    throw sp::InvalidArgumentError(("Workspace: " + text).c_str());
}

// A query by names, as Python passes it (objects.py _native_query): (layer, stage,
// intersecting, within rings, within predicate, label, at_least pairs, at_most pairs,
// include_in_flight, limit). The polygon is built with the GIL held; the names are resolved
// without it.
struct QueryArgs {
    std::optional<std::string> layer;
    std::optional<std::string> stage;
    std::optional<sp::Rect64> intersecting;
    std::optional<sp::Polygon> within;
    sp::SpatialPredicate predicate = sp::SpatialPredicate::Intersects;
    std::optional<std::string> label;
    std::vector<std::pair<std::string, double>> atLeast;
    std::vector<std::pair<std::string, double>> atMost;
    bool includeInFlight = false;
    std::optional<int64_t> limit;
};

QueryArgs queryArgsOf(const py::tuple& t) {
    if (t.size() != 10) refuse("a query must be a 10-tuple");
    QueryArgs a;
    a.layer = t[0].cast<std::optional<std::string>>();
    a.stage = t[1].cast<std::optional<std::string>>();
    if (!t[2].is_none()) {
        const auto r = t[2].cast<std::tuple<int64_t, int64_t, int64_t, int64_t>>();
        a.intersecting = sp::Rect64{std::get<0>(r), std::get<1>(r), std::get<2>(r), std::get<3>(r)};
    }
    if (!t[3].is_none()) {
        sp::Polygon polygon;
        for (const py::handle ring : t[3].cast<py::list>()) {
            const auto pair = ring.cast<py::tuple>();
            const auto points = pair[0].cast<py::array_t<int64_t, py::array::c_style | py::array::forcecast>>();
            if (points.ndim() != 2 || points.shape(1) != 2) refuse("a ring's points must have shape (n, 2)");
            std::vector<sp::Point64> vertices(static_cast<size_t>(points.shape(0)));
            for (size_t i = 0; i < vertices.size(); ++i) {
                vertices[i] = sp::Point64{points.at(static_cast<py::ssize_t>(i), 0),
                                          points.at(static_cast<py::ssize_t>(i), 1)};
            }
            polygon.addRing(vertices.data(), static_cast<int64_t>(vertices.size()), pair[1].cast<bool>());
        }
        a.within = std::move(polygon);
    }
    const auto predicate = t[4].cast<int32_t>();
    if (predicate != static_cast<int32_t>(sp::SpatialPredicate::Intersects) &&
        predicate != static_cast<int32_t>(sp::SpatialPredicate::ContainedBy)) {
        refuse("the spatial predicate " + std::to_string(predicate) + " is not one of SpatialPredicate");
    }
    a.predicate = static_cast<sp::SpatialPredicate>(predicate);
    a.label = t[5].cast<std::optional<std::string>>();
    a.atLeast = t[6].cast<std::vector<std::pair<std::string, double>>>();
    a.atMost = t[7].cast<std::vector<std::pair<std::string, double>>>();
    a.includeInFlight = t[8].cast<bool>();
    a.limit = t[9].cast<std::optional<int64_t>>();
    return a;
}

// Resolves the names and builds the library's query. Call without the GIL.
sp::ObjectQuery buildQuery(sp::Workspace& workspace, const QueryArgs& a) {
    sp::ObjectQuery q;
    if (a.layer) {
        sp::LayerId id;
        if (!workspace.layers().find(a.layer->c_str(), id)) refuse("no layer named '" + *a.layer + "'");
        q.layer(id);
    }
    if (a.stage) {
        sp::StageId id;
        if (!workspace.stages().find(a.stage->c_str(), id)) refuse("no stage named '" + *a.stage + "'");
        q.stage(id);
    }
    if (a.intersecting) q.intersecting(*a.intersecting);
    if (a.within) q.withinPolygon(*a.within, a.predicate);
    if (a.label) {
        sp::LabelId id;
        if (!workspace.store()->vocabulary().find(a.label->c_str(), id)) refuse("no label named '" + *a.label + "'");
        q.label(id);
    }
    for (const auto& [name, value] : a.atLeast) q.measurementAtLeast(name.c_str(), value);
    for (const auto& [name, value] : a.atMost) q.measurementAtMost(name.c_str(), value);
    if (a.includeInFlight) q.visibility(sp::StageVisibility::IncludeInFlight);
    if (a.limit) q.limit(*a.limit);
    return q;
}

std::string idText(const sp::ObjectId& id) {
    char buffer[64];
    const int32_t length = id.toString(buffer, static_cast<int32_t>(sizeof(buffer)));
    if (length < static_cast<int32_t>(sizeof(buffer))) return std::string(buffer, static_cast<size_t>(length));
    std::string text(static_cast<size_t>(length) + 1, '\0');
    id.toString(text.data(), length + 1);
    text.resize(static_cast<size_t>(length));
    return text;
}

using Ring = std::pair<std::vector<sp::Point64>, bool>;

// Records read with the GIL released, names already resolved, waiting to become Python
// objects. names given: values holds names.size() columns of count values, NaN when absent;
// otherwise each record's measurements are in perRecord.
struct Batch {
    explicit Batch(const std::optional<std::vector<std::string>>& requested) : names(requested) {}

    std::optional<std::vector<std::string>> names;
    bool withGeometry = false;
    size_t count = 0;
    std::vector<std::string> ids;
    std::vector<sp::LayerId> layers;
    std::vector<sp::StageId> stages;
    std::vector<sp::LabelId> labels;
    std::vector<int64_t> bbox;
    std::vector<std::vector<double>> values;
    std::vector<std::vector<std::pair<std::string, double>>> perRecord;
    std::vector<std::vector<Ring>> geometry;
    std::map<int32_t, std::string> layerNames;
    std::map<int32_t, std::string> stageNames;
    std::map<int32_t, std::string> labelNames;

    void add(const sp::ObjectRecord& record) {
        ids.push_back(idText(record.id()));
        layers.push_back(record.layer());
        stages.push_back(record.stage());
        labels.push_back(record.hasLabel() ? record.label() : sp::LabelId());
        const sp::Rect64 box = record.boundingBox();
        bbox.insert(bbox.end(), {box.x, box.y, box.width, box.height});
        const sp::MeasurementSet& set = record.measurements();
        if (names) {
            if (values.empty()) values.resize(names->size());
            for (size_t k = 0; k < names->size(); ++k) {
                sp::Measurement m;
                values[k].push_back(set.find((*names)[k].c_str(), m) ? m.value()
                                                                     : std::numeric_limits<double>::quiet_NaN());
            }
        } else {
            std::vector<std::pair<std::string, double>> own;
            const int32_t n = set.count();
            own.reserve(static_cast<size_t>(n));
            for (int32_t i = 0; i < n; ++i) {
                const sp::Measurement m = set.at(i);
                own.emplace_back(m.name(), m.value());
            }
            perRecord.push_back(std::move(own));
        }
        if (withGeometry) {
            const sp::Polygon& polygon = record.geometry();
            std::vector<Ring> rings;
            const int32_t ringCount = polygon.ringCount();
            for (int32_t r = 0; r < ringCount; ++r) {
                std::vector<sp::Point64> points(static_cast<size_t>(polygon.ringPointCount(r)));
                polygon.ringPoints(r, points.data(), static_cast<int64_t>(points.size()));
                rings.emplace_back(std::move(points), polygon.ringIsHole(r));
            }
            geometry.push_back(std::move(rings));
        }
        ++count;
    }

    // Looks up every layer, stage and label name the records use.
    void resolveNames(sp::Workspace& workspace) {
        for (const sp::LayerId id : layers) {
            if (layerNames.count(id.value()) == 0) layerNames.emplace(id.value(), workspace.layers().name(id));
        }
        for (const sp::StageId id : stages) {
            if (stageNames.count(id.value()) == 0) stageNames.emplace(id.value(), workspace.stages().name(id));
        }
        for (const sp::LabelId id : labels) {
            if (id.isValid() && labelNames.count(id.value()) == 0) {
                labelNames.emplace(id.value(), workspace.store()->vocabulary().name(id));
            }
        }
    }

    // The batch as Python: (ids, layers, stages, bbox, labels, measurements, geometry), where
    // measurements is {name: float64 array} when names were given and a list of per-record
    // dicts otherwise, and geometry is a list of per-record ring lists or None. Call with the
    // GIL held.
    py::tuple toPython() const {
        const auto n = static_cast<py::ssize_t>(count);
        auto namesOf = [n](const auto& ids, const std::map<int32_t, std::string>& table) {
            std::map<int32_t, py::str> cache;
            for (const auto& [value, name] : table) cache.emplace(value, py::str(name));
            py::list out(n);
            for (py::ssize_t i = 0; i < n; ++i) out[i] = cache.at(ids[static_cast<size_t>(i)].value());
            return out;
        };
        py::list idList(n);
        for (py::ssize_t i = 0; i < n; ++i) idList[i] = py::str(ids[static_cast<size_t>(i)]);
        py::list labelList(n);
        for (py::ssize_t i = 0; i < n; ++i) {
            const sp::LabelId id = labels[static_cast<size_t>(i)];
            labelList[i] = id.isValid() ? py::object(py::str(labelNames.at(id.value()))) : py::object(py::none());
        }
        py::array_t<int64_t> box({n, static_cast<py::ssize_t>(4)});
        if (!bbox.empty()) std::memcpy(box.mutable_data(), bbox.data(), bbox.size() * sizeof(int64_t));

        py::object measurements;
        if (names) {
            py::dict columns;
            for (size_t k = 0; k < names->size(); ++k) {
                py::array_t<double> column(n);
                if (n > 0) std::memcpy(column.mutable_data(), values[k].data(), values[k].size() * sizeof(double));
                columns[py::str((*names)[k])] = column;
            }
            measurements = columns;
        } else {
            py::list rows(n);
            for (py::ssize_t i = 0; i < n; ++i) {
                py::dict row;
                for (const auto& [name, value] : perRecord[static_cast<size_t>(i)]) row[py::str(name)] = value;
                rows[i] = row;
            }
            measurements = rows;
        }

        py::object shapes = py::none();
        if (withGeometry) {
            py::list all(n);
            for (py::ssize_t i = 0; i < n; ++i) {
                const auto& rings = geometry[static_cast<size_t>(i)];
                py::list out;
                for (const Ring& ring : rings) {
                    const auto count = static_cast<py::ssize_t>(ring.first.size());
                    py::array_t<int64_t> points({count, static_cast<py::ssize_t>(2)});
                    for (py::ssize_t p = 0; p < count; ++p) {
                        points.mutable_at(p, 0) = ring.first[static_cast<size_t>(p)].x;
                        points.mutable_at(p, 1) = ring.first[static_cast<size_t>(p)].y;
                    }
                    out.append(py::make_tuple(points, py::bool_(ring.second)));
                }
                all[i] = out;
            }
            shapes = all;
        }
        return py::make_tuple(idList, namesOf(layers, layerNames), namesOf(stages, stageNames), box, labelList,
                              measurements, shapes);
    }
};

// A cursor over a query's result. It keeps the workspace alive (decision 7): a cursor whose
// workspace was closed raises the library's IoError on its next fetch. The library's cursor
// is ThreadConfined, so a mutex, taken without the GIL, serialises fetches from several
// Python threads.
class CursorHandle {
public:
    CursorHandle(WorkspaceHolder workspace, sp::CursorPtr cursor)
        : workspace_(std::move(workspace)), cursor_(std::move(cursor)) {}

    CursorHandle(const CursorHandle&) = delete;
    CursorHandle& operator=(const CursorHandle&) = delete;

    // Closes the cursor and drops the workspace with the GIL released: both may wait on
    // library locks.
    ~CursorHandle() {
        if (Py_IsInitialized() != 0 && PyGILState_Check() != 0) {
            py::gil_scoped_release release;
            drop();
        } else {
            drop();
        }
    }

    // Up to n records, or None once the result is exhausted or the cursor closed.
    py::object fetch(int64_t n, const std::optional<std::vector<std::string>>& names, bool withGeometry) {
        if (n < 1) refuse("a fetch needs n of at least 1");
        Batch batch(names);
        batch.withGeometry = withGeometry;
        {
            py::gil_scoped_release release;
            std::lock_guard<std::mutex> lock(mutex_);
            while (cursor_ && !exhausted_ && static_cast<int64_t>(batch.count) < n) {
                if (!cursor_->next()) {
                    exhausted_ = true;
                    break;
                }
                batch.add(cursor_->current());
            }
            if (batch.count > 0) batch.resolveNames(*workspace_);
        }
        if (batch.count == 0) return py::none();
        return batch.toPython();
    }

    // Closes the cursor and releases the workspace. A second call does nothing. A failure
    // of the library's close (a cursor of a closed store) is swallowed: the cursor is
    // released either way.
    void close() {
        py::gil_scoped_release release;
        std::lock_guard<std::mutex> lock(mutex_);
        drop();
    }

private:
    void drop() noexcept {
        if (cursor_) {
            try {
                cursor_->close();
            } catch (...) {  // NOLINT(bugprone-empty-catch): nothing to report it to
            }
            cursor_.reset();
        }
        workspace_.reset();
    }

    std::mutex mutex_;
    WorkspaceHolder workspace_;
    sp::CursorPtr cursor_;
    bool exhausted_ = false;
};

} // namespace

void bindObjects(py::module_& m) {
    py::class_<CursorHandle>(m, "_Cursor")
        .def("fetch", &CursorHandle::fetch, py::arg("n"), py::arg("names"), py::arg("with_geometry"))
        .def("close", &CursorHandle::close);

    m.def(
        "_query",
        [](const WorkspaceHolder& workspace, const py::tuple& query) {
            QueryArgs args = queryArgsOf(query);
            py::gil_scoped_release release;
            sp::CursorPtr cursor = workspace->store()->objects().query(buildQuery(*workspace, args));
            return std::make_unique<CursorHandle>(workspace, std::move(cursor));
        },
        py::arg("workspace"), py::arg("query"));
    m.def(
        "_count",
        [](const WorkspaceHolder& workspace, const py::tuple& query) {
            QueryArgs args = queryArgsOf(query);
            py::gil_scoped_release release;
            return workspace->store()->objects().count(buildQuery(*workspace, args));
        },
        py::arg("workspace"), py::arg("query"));
    m.def(
        "_get",
        [](const WorkspaceHolder& workspace, const std::string& text) -> py::object {
            sp::ObjectId id;
            if (!sp::ObjectId::fromString(text.c_str(), id)) refuse("'" + text + "' is not an object id");
            Batch batch(std::nullopt);
            batch.withGeometry = true;
            {
                py::gil_scoped_release release;
                sp::ObjectRecord record;
                if (workspace->store()->objects().get(id, record)) {
                    batch.add(record);
                    batch.resolveNames(*workspace);
                }
            }
            if (batch.count == 0) return py::none();
            return batch.toPython();
        },
        py::arg("workspace"), py::arg("id"));
    m.def(
        "_measurement_names",
        [](const WorkspaceHolder& workspace) {
            std::vector<std::string> names;
            {
                py::gil_scoped_release release;
                sp::ObjectStore& objects = workspace->store()->objects();
                const int32_t count = objects.measurementNameCount();
                for (int32_t i = 0; i < count; ++i) names.emplace_back(objects.measurementNameAt(i));
            }
            return names;
        },
        py::arg("workspace"));
}

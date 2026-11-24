// Python operations: a C++ stand-in factory in the library's OperationCatalog for each
// registered subclass of slideio_processor.Operation.
//
// GIL rules: no OperationFactory method touches Python; it answers from a snapshot of the
// class's declarations. A stand-in operation takes the GIL only around building the Python
// instance, apply, the array conversions and recording an error; it waits on its locks
// without the GIL.
#include "bindings.hpp"
#include "python_runtime.hpp"

#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include <slideio/processor/core/block_view.hpp>
#include <slideio/processor/core/data_type.hpp>
#include <slideio/processor/core/error.hpp>
#include <slideio/processor/pipeline/operation.hpp>
#include <slideio/processor/pipeline/operation_catalog.hpp>
#include <slideio/processor/pipeline/operation_parameters.hpp>

#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

namespace sp = slideio::processor;
using namespace slideio_processor_python;

enum class Kind { Bool, Int, Double, String, DoubleArray };

// A class's declarations, as Operation.__init_subclass__ checked them.
struct Snapshot {
    PyObject* cls = nullptr; // a strong reference, never released (decision 9)
    std::string version;
    std::string origin;
    bool labels = false;
    int32_t halo = 0;
    bool deterministic = true;
    int32_t maxConcurrency = 0; // 0: unlimited
    std::map<std::string, Kind> kinds;
    sp::DataType outputType = sp::DataType::Float32;
    int32_t outputPlanes = 1;
    int64_t workingSetBytesPerPixel = 0;
};
using SnapshotPtr = std::shared_ptr<const Snapshot>;

const std::map<std::string, Kind>& kindNames() {
    static const std::map<std::string, Kind> names{{"bool", Kind::Bool},
                                                   {"int", Kind::Int},
                                                   {"double", Kind::Double},
                                                   {"string", Kind::String},
                                                   {"doubleArray", Kind::DoubleArray}};
    return names;
}

const char* kindName(Kind kind) {
    switch (kind) {
    case Kind::Bool: return "a bool";
    case Kind::Int: return "an int";
    case Kind::Double: return "a float";
    case Kind::String: return "a string";
    case Kind::DoubleArray: return "an array of numbers";
    }
    return "?";
}

// The kind of the value a bag holds under a key it has. The bag has five setters, so a
// value that is none of the four scalars is a double array (possibly empty).
Kind kindOf(const sp::OperationParameters& parameters, const char* key) {
    bool b = false;
    int64_t i = 0;
    double d = 0;
    if (parameters.getBool(key, b)) return Kind::Bool;
    if (parameters.getInt(key, i)) return Kind::Int;
    if (parameters.getDouble(key, d)) return Kind::Double;
    if (parameters.getString(key, nullptr) != nullptr) return Kind::String;
    return Kind::DoubleArray;
}

sp::DataType dataTypeNamed(const std::string& name) {
    for (int32_t t = 0; t < static_cast<int32_t>(sp::DataType::Unknown); ++t) {
        const auto type = static_cast<sp::DataType>(t);
        if (name == sp::dataTypeName(type)) return type;
    }
    throw py::value_error("unknown data type " + name);
}

// What apply's `context` sees of the tile; read-only. The token is the run's and valid only
// during apply: the stand-in clears it afterwards, and `cancelled` then reads true.
struct TileContext {
    int32_t level = 0;
    sp::Rect64 tileRect{};
    sp::Rect64 haloRect{};
    double levelScale = 1;
    double pixelSizeUm = 0;
    const sp::CancellationToken* token = nullptr; // read and cleared with the GIL held

    bool cancelled() const noexcept { return token == nullptr || token->isCancelled(); }
};

py::tuple rectTuple(const sp::Rect64& r) { return py::make_tuple(r.x, r.y, r.width, r.height); }

// The stage's values of the declared parameters, as a dict; "version" and "targetLayer"
// are left out. Call with the GIL held.
py::dict parameterValues(const sp::OperationParameters& parameters, const Snapshot& snapshot) {
    py::dict values;
    for (int32_t i = 0; i < parameters.count(); ++i) {
        const char* key = parameters.keyAt(i);
        const auto declared = snapshot.kinds.find(key);
        if (declared == snapshot.kinds.end()) continue;
        switch (kindOf(parameters, key)) {
        case Kind::Bool: {
            bool b = false;
            parameters.getBool(key, b);
            values[key] = b;
            break;
        }
        case Kind::Int: {
            int64_t v = 0;
            parameters.getInt(key, v);
            if (declared->second == Kind::Double) values[key] = static_cast<double>(v);
            else values[key] = v;
            break;
        }
        case Kind::Double: {
            double v = 0;
            parameters.getDouble(key, v);
            values[key] = v;
            break;
        }
        case Kind::String: values[key] = std::string(parameters.getString(key, "")); break;
        case Kind::DoubleArray: {
            std::vector<double> v(static_cast<size_t>(parameters.getDoubleArray(key, nullptr, 0)));
            parameters.getDoubleArray(key, v.data(), static_cast<int32_t>(v.size()));
            values[key] = v;
            break;
        }
        }
    }
    return values;
}

// Clears the token of apply's context however the call ends: the token is the run's and
// valid only during apply. Destroyed with the GIL held, before the context object.
class TokenClear {
public:
    explicit TokenClear(TileContext& context) noexcept : context_(context) {}
    ~TokenClear() { context_.token = nullptr; }
    TokenClear(const TokenClear&) = delete;
    TokenClear& operator=(const TokenClear&) = delete;

private:
    TileContext& context_;
};

// The Python side of one stand-in operation: the instance, built on its first tile rather
// than when the stand-in is created (the library creates operations several times per run),
// and the call of apply. Every declaration comes from the stand-in's own snapshot, so a run
// keeps the class it started with when the class is redefined meanwhile (decision 2).
class PythonInstance {
public:
    // Receives what apply returned, with the GIL held; returns a failure text, or "".
    using Take = std::function<std::string(const py::object& returned)>;

    PythonInstance(std::string name, SnapshotPtr snapshot, std::shared_ptr<ConcurrencyGate> gate,
                   const sp::OperationParameters& parameters)
        : name_(std::move(name)), snapshot_(std::move(snapshot)), gate_(std::move(gate)), parameters_(parameters) {}

    ~PythonInstance() {
        if (instance_.ptr() == nullptr) return;
        if (enterPython()) {
            {
                py::gil_scoped_acquire gil;
                instance_ = py::object();
            }
            leavePython();
        } else {
            instance_.release(); // the interpreter is exiting or gone: leak it (decision 9)
        }
    }

    PythonInstance(const PythonInstance&) = delete;
    PythonInstance& operator=(const PythonInstance&) = delete;

    const std::string& name() const { return name_; }
    const Snapshot& snapshot() const { return *snapshot_; }
    const sp::OperationParameters& parameters() const { return parameters_; }

    // Calls apply with a copy of the context's input and hands its result to `take`. Throws
    // std::runtime_error with the failure text when anything fails.
    void apply(const sp::OperationContext& context, const Take& take) {
        ensureInstance();
        GatePass pass(*gate_, snapshot_->maxConcurrency);
        const sp::BlockView& in = context.input();

        TileContext tileContext;
        tileContext.level = context.level();
        tileContext.tileRect = context.tileRectAtLevel();
        tileContext.haloRect = context.haloRectAtLevel();
        tileContext.levelScale = context.levelScale();
        tileContext.pixelSizeUm = context.pixelSizeMicrons();
        tileContext.token = &context.cancellation();

        std::string failure;
        {
            PythonScope python(name_);
            try {
                py::array tile(py::dtype(numpyName(in.planeType(0))),
                               std::vector<py::ssize_t>{in.height(), in.width(), in.planeCount()});
                {
                    // Only this thread knows the new array: fill it without the GIL.
                    py::gil_scoped_release release;
                    fillTile(in, static_cast<char*>(tile.mutable_data()));
                }
                py::object returned;
                {
                    const py::object contextObject = py::cast(tileContext);
                    TokenClear clear(contextObject.cast<TileContext&>());
                    try {
                        returned = instance_.attr("apply")(tile, contextObject);
                    } catch (py::error_already_set& e) {
                        failure = describe(e);
                        recordError(name_, e);
                    }
                }
                if (failure.empty()) failure = take(returned);
            } catch (py::error_already_set& e) {
                failure = "operation '" + name_ + "': " + describe(e);
            }
        }
        if (!failure.empty()) throw std::runtime_error(failure);
    }

private:
    enum State : int { Pending, Ready, Failed };

    // Decision 4: the mutex is taken without the GIL, then the GIL inside it. A failed
    // construction is kept: later tiles fail with the same text and __init__ runs once.
    void ensureInstance() {
        int state = state_.load(std::memory_order_acquire);
        if (state == Ready) return;
        if (state == Failed) throw std::runtime_error(initFailure_);
        std::lock_guard<std::mutex> lock(instanceMutex_);
        state = state_.load(std::memory_order_relaxed);
        if (state == Ready) return;
        if (state == Failed) throw std::runtime_error(initFailure_);
        std::string failure;
        {
            PythonScope python(name_);
            try {
                py::handle cls(snapshot_->cls);
                instance_ = py::module_::import("slideio_processor.operation")
                                .attr("_instantiate")(cls, parameterValues(parameters_, *snapshot_));
            } catch (py::error_already_set& e) {
                failure = describe(e);
                recordError(name_, e);
            }
        }
        if (!failure.empty()) {
            initFailure_ = failure;
            state_.store(Failed, std::memory_order_release);
            throw std::runtime_error(failure);
        }
        state_.store(Ready, std::memory_order_release);
    }

    const std::string name_;
    const SnapshotPtr snapshot_;
    const std::shared_ptr<ConcurrencyGate> gate_; // the registered name's (decision 8)
    const sp::OperationParameters parameters_;
    std::mutex instanceMutex_;
    std::atomic<int> state_{Pending};
    std::string initFailure_; // written once, before state_ becomes Failed
    py::object instance_;     // set once under instanceMutex_ and the GIL
};

// The operation a run gets for a RASTER class: apply's array, cropped to the tile rect, is
// the output.
class RasterStandIn final : public sp::Operation {
public:
    RasterStandIn(std::string name, SnapshotPtr snapshot, std::shared_ptr<ConcurrencyGate> gate,
                  const sp::OperationParameters& parameters)
        : python_(std::move(name), std::move(snapshot), std::move(gate), parameters) {}

    const char* name() const override { return python_.name().c_str(); }
    sp::ChannelArity arity() const override { return sp::ChannelArity::Joint; }
    bool requiresUniformTypes() const override { return true; }
    sp::DataTypeSet supportedTypes() const override { return sp::DataTypeSet::all(); }
    sp::Halo halo() const override { return sp::Halo::uniform(python_.snapshot().halo); }
    sp::DataType outputType(sp::DataType) const override { return python_.snapshot().outputType; }
    const sp::OperationParameters& parameters() const override { return python_.parameters(); }
    int32_t outputPlaneCount(int32_t) const override { return python_.snapshot().outputPlanes; }
    int64_t workingSetBytesPerLevelPixel() const override { return python_.snapshot().workingSetBytesPerPixel; }

    void apply(sp::OperationContext& context) override {
        const Snapshot& s = python_.snapshot();
        const sp::BlockView& in = context.input();
        const sp::Rect64 tileRect = context.tileRectAtLevel();
        const sp::Rect64 haloRect = context.haloRectAtLevel();
        python_.apply(context, [&](const py::object& returned) {
            return copyResult(python_.name(), returned, s.outputType, s.outputPlanes, in.height(), in.width(),
                              tileRect.x - haloRect.x, tileRect.y - haloRect.y, context.output());
        });
    }

private:
    PythonInstance python_;
};


// The catalog's factory for one registered name. A redefinition swaps its snapshot.
class PythonOperationFactory final : public sp::OperationFactory {
public:
    PythonOperationFactory(std::string name, SnapshotPtr snapshot) : name_(std::move(name)) { replace(snapshot); }

    void replace(SnapshotPtr snapshot) {
        std::lock_guard<std::mutex> lock(mutex_);
        // Every snapshot is kept, so a pointer origin() returned stays valid.
        history_.push_back(snapshot);
        current_ = std::move(snapshot);
    }

    SnapshotPtr snapshot() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return current_;
    }

    sp::OperationPtr create(const sp::OperationParameters& parameters) const override {
        SnapshotPtr s = snapshot();
        check(parameters, *s);
        if (s->labels) {
            // Task 4 adds the LABELS stand-in.
            throw sp::InvalidArgumentError(("operation '" + name_ + "': LABELS operations cannot run yet").c_str());
        }
        return std::make_shared<RasterStandIn>(name_, std::move(s), gate_, parameters);
    }

    const char* name() const override { return name_.c_str(); }
    sp::ChannelArity arity() const override { return sp::ChannelArity::Joint; }
    bool requiresUniformTypes() const override { return true; }
    sp::DataTypeSet supportedTypes() const override { return sp::DataTypeSet::all(); }
    // These answer from the current snapshot: what the next run gets. A stand-in already
    // created answers from its own, and the library takes the stand-in's word (decision 2).
    sp::Halo halo() const override { return sp::Halo::uniform(snapshot()->halo); }
    const char* correctnessReference() const override { return "a Python class; see its origin"; }
    const char* complexityNote() const override { return ""; }
    int64_t workingSetBytesPerLevelPixel() const override { return snapshot()->workingSetBytesPerPixel; }
    bool producesObjects() const override { return snapshot()->labels; }
    bool isDeterministic() const override { return snapshot()->deterministic; }
    const char* origin() const override { return snapshot()->origin.c_str(); }
    int32_t outputPlaneCount(int32_t) const override { return snapshot()->outputPlanes; }

private:
    [[noreturn]] void refuse(const std::string& text) const {
        sp::ErrorContext context;
        context.operationName = name_.c_str();
        throw sp::InvalidArgumentError(("operation '" + name_ + "': " + text).c_str(), context);
    }

    // Global Constraints and decision 3. Messages name keys, never values (the version
    // excepted).
    void check(const sp::OperationParameters& parameters, const Snapshot& s) const {
        bool hasVersion = false;
        for (int32_t i = 0; i < parameters.count(); ++i) {
            const std::string key = parameters.keyAt(i);
            const Kind kind = kindOf(parameters, key.c_str());
            if (key == "version") {
                if (kind != Kind::String) refuse("the stage parameter 'version' must be a string");
                const std::string version = parameters.getString("version", "");
                if (version != s.version) {
                    refuse("the stage carries version '" + version + "', the class has version '" + s.version + "'");
                }
                hasVersion = true;
                continue;
            }
            if (key == "targetLayer" && s.labels) {
                if (kind != Kind::String) refuse("the stage parameter 'targetLayer' must be a string");
                continue;
            }
            const auto declared = s.kinds.find(key);
            if (declared == s.kinds.end()) refuse("unknown stage parameter '" + key + "'");
            if (kind != declared->second && !(declared->second == Kind::Double && kind == Kind::Int)) {
                refuse("the stage parameter '" + key + "' must be " + kindName(declared->second));
            }
        }
        if (!hasVersion) refuse("the stage parameters carry no 'version'; the class has version '" + s.version + "'");
    }

    const std::string name_;
    const std::shared_ptr<ConcurrencyGate> gate_ = std::make_shared<ConcurrencyGate>(); // per name
    mutable std::mutex mutex_;
    SnapshotPtr current_;
    std::vector<SnapshotPtr> history_;
};

// The registered Python operations. Never destroyed: the catalog, which shares the
// factories, is destroyed after Python has finalized (decision 9).
std::mutex registryMutex;
std::map<std::string, std::shared_ptr<PythonOperationFactory>>& registry() {
    static auto* factories = new std::map<std::string, std::shared_ptr<PythonOperationFactory>>();
    return *factories;
}

SnapshotPtr toSnapshot(const py::handle& cls, const py::dict& d) {
    auto s = std::make_shared<Snapshot>();
    s->version = d["version"].cast<std::string>();
    s->origin = d["origin"].cast<std::string>();
    s->labels = d["output"].cast<std::string>() == "labels";
    s->halo = d["halo"].cast<int32_t>();
    s->deterministic = d["deterministic"].cast<bool>();
    s->maxConcurrency = d["max_concurrency"].cast<int32_t>();
    for (const auto& item : d["parameter_kinds"].cast<py::dict>()) {
        s->kinds[item.first.cast<std::string>()] = kindNames().at(item.second.cast<std::string>());
    }
    s->outputType = dataTypeNamed(d["output_type"].cast<std::string>());
    s->outputPlanes = d["output_planes"].cast<int32_t>();
    s->workingSetBytesPerPixel = d["working_set_bytes_per_pixel"].cast<int64_t>();
    s->cls = cls.inc_ref().ptr();
    return s;
}

void registerPythonOperation(const py::handle& cls, const py::dict& snapshot) {
    const std::string name = snapshot["name"].cast<std::string>();
    std::lock_guard<std::mutex> lock(registryMutex);
    auto& factories = registry();
    const auto found = factories.find(name);
    if (found != factories.end()) {
        const bool labels = snapshot["output"].cast<std::string>() == "labels";
        if (found->second->snapshot()->labels != labels) {
            throw sp::InvalidArgumentError(("operation '" + name + "': a redefinition cannot change output from " +
                                            (labels ? "RASTER to LABELS" : "LABELS to RASTER"))
                                               .c_str());
        }
        found->second->replace(toSnapshot(cls, snapshot));
        return;
    }
    sp::OperationCatalog& catalog = sp::OperationCatalog::instance();
    if (catalog.isRegistered(name.c_str())) {
        throw sp::InvalidArgumentError(
            ("operation '" + name + "': the name belongs to a built-in operation").c_str());
    }
    auto factory = std::make_shared<PythonOperationFactory>(name, toSnapshot(cls, snapshot));
    catalog.registerFactory(factory);
    factories.emplace(name, std::move(factory));
}

} // namespace

void bindOperations(py::module_& m) {
    py::class_<TileContext>(m, "TileContext",
                            "What apply's context sees of the tile: read-only, and valid only during apply.")
        .def_readonly("level", &TileContext::level, "The analysis level, 0 for full resolution.")
        .def_property_readonly(
            "tile_rect", [](const TileContext& c) { return rectTuple(c.tileRect); },
            "(x, y, width, height) of the tile at the analysis level.")
        .def_property_readonly(
            "halo_rect", [](const TileContext& c) { return rectTuple(c.haloRect); },
            "(x, y, width, height) of the tile grown by the halo, at the analysis level; not clipped to the level.")
        .def_readonly("level_scale", &TileContext::levelScale, "The factor from the analysis level to level 0.")
        .def_readonly("pixel_size_um", &TileContext::pixelSizeUm,
                      "The level-0 pixel size in micrometres; 0 when unknown.")
        .def_property_readonly("cancelled", &TileContext::cancelled,
                               "True once the run is cancelled; always True after apply has returned.");
    // Python runs atexit handlers after joining its non-daemon threads and before it begins
    // finalizing: from then on the stand-ins stay out of Python (decision 9).
    py::module_::import("atexit").attr("register")(py::cpp_function(&closePython));
    m.def("_register_python_operation", &registerPythonOperation, py::arg("cls"), py::arg("snapshot"),
          "Registers, or redefines, a Python operation from its class and snapshot.");
    m.def(
        "_catalog_names",
        []() {
            const sp::OperationCatalog& catalog = sp::OperationCatalog::instance();
            std::vector<std::string> names;
            for (int32_t i = 0; i < catalog.count(); ++i) names.emplace_back(catalog.nameAt(i));
            return names;
        },
        "The catalog's operation names, in its order.");
    m.def(
        "_is_python_operation",
        [](const std::string& name) {
            std::lock_guard<std::mutex> lock(registryMutex);
            return registry().count(name) != 0;
        },
        py::arg("name"), "True when a Python class registered the name.");
}

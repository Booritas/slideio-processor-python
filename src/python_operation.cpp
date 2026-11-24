// Python operations: a C++ stand-in factory in the library's OperationCatalog for each
// registered subclass of slideio_processor.Operation.
//
// GIL rules: no OperationFactory method touches Python; it answers from a snapshot of the
// class's declarations. A stand-in operation takes the GIL only around building the Python
// instance, apply and the array conversions.
#include "bindings.hpp"

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
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

namespace sp = slideio::processor;

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

const char* numpyName(sp::DataType type) {
    switch (type) {
    case sp::DataType::Byte: return "uint8";
    case sp::DataType::Int8: return "int8";
    case sp::DataType::UInt16: return "uint16";
    case sp::DataType::Int16: return "int16";
    case sp::DataType::UInt32: return "uint32";
    case sp::DataType::Int32: return "int32";
    case sp::DataType::UInt64: return "uint64";
    case sp::DataType::Int64: return "int64";
    case sp::DataType::Float16: return "float16";
    case sp::DataType::Float32: return "float32";
    case sp::DataType::Float64: return "float64";
    default: break;
    }
    throw std::runtime_error("no NumPy dtype for this data type");
}

bool interpreterAlive() {
#if PY_VERSION_HEX >= 0x030D0000
    return Py_IsInitialized() && !Py_IsFinalizing();
#else
    return Py_IsInitialized() && !_Py_IsFinalizing();
#endif
}

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

// The text of the Python exception being handled, "<Type>: <str>". Call with the GIL held.
std::string describe(const py::error_already_set& e) {
    std::string text = py::str(py::type::of(e.value()).attr("__name__"));
    const std::string message = py::str(e.value());
    if (!message.empty()) text += ": " + message;
    return text;
}

// The operation a run gets for a RASTER class. It builds the Python instance on its
// first tile, not when it is created: the library creates operations several times per run.
class RasterStandIn final : public sp::Operation {
public:
    RasterStandIn(std::string name, SnapshotPtr snapshot, const sp::OperationParameters& parameters)
        : name_(std::move(name)), snapshot_(std::move(snapshot)), parameters_(parameters) {}

    ~RasterStandIn() override {
        if (instance_.ptr() == nullptr) return;
        if (interpreterAlive()) {
            py::gil_scoped_acquire gil;
            instance_ = py::object();
        } else {
            instance_.release(); // the interpreter is gone: leak it (decision 9)
        }
    }

    const char* name() const override { return name_.c_str(); }
    sp::ChannelArity arity() const override { return sp::ChannelArity::Joint; }
    bool requiresUniformTypes() const override { return true; }
    sp::DataTypeSet supportedTypes() const override { return sp::DataTypeSet::all(); }
    sp::Halo halo() const override { return sp::Halo::uniform(snapshot_->halo); }
    sp::DataType outputType(sp::DataType) const override { return snapshot_->outputType; }
    const sp::OperationParameters& parameters() const override { return parameters_; }
    int32_t outputPlaneCount(int32_t) const override { return snapshot_->outputPlanes; }
    int64_t workingSetBytesPerLevelPixel() const override { return snapshot_->workingSetBytesPerPixel; }

    void apply(sp::OperationContext& context) override {
        ensureInstance();
        const sp::BlockView& in = context.input();
        sp::BlockView& out = context.output();
        const int64_t height = in.height();
        const int64_t width = in.width();
        const int32_t planes = in.planeCount();
        const sp::DataType inType = in.planeType(0);
        const auto inSize = static_cast<size_t>(sp::dataTypeSizeBytes(inType));
        const int64_t offsetX = context.tileRectAtLevel().x - context.haloRectAtLevel().x;
        const int64_t offsetY = context.tileRectAtLevel().y - context.haloRectAtLevel().y;

        std::string failure;
        {
            py::gil_scoped_acquire gil;
            try {
                py::array tile(py::dtype(numpyName(inType)), std::vector<py::ssize_t>{height, width, planes});
                auto* dst = static_cast<char*>(tile.mutable_data());
                for (int64_t y = 0; y < height; ++y) {
                    for (int32_t p = 0; p < planes; ++p) {
                        const char* src = static_cast<const char*>(in.planeData(p)) + y * in.planeRowStrideBytes(p);
                        for (int64_t x = 0; x < width; ++x) {
                            std::memcpy(dst + ((y * width + x) * planes + p) * inSize, src + x * inSize, inSize);
                        }
                    }
                }
                const py::object returned = instance_.attr("apply")(tile, py::none());
                failure = copyOut(returned, height, width, offsetX, offsetY, out);
            } catch (py::error_already_set& e) {
                failure = describe(e);
            }
        }
        if (!failure.empty()) throw std::runtime_error(failure);
    }

private:
    // Decision 4: the mutex is taken without the GIL, then the GIL inside it.
    void ensureInstance() {
        if (ready_.load(std::memory_order_acquire)) return;
        std::lock_guard<std::mutex> lock(instanceMutex_);
        if (ready_.load(std::memory_order_relaxed)) return;
        std::string failure;
        {
            py::gil_scoped_acquire gil;
            try {
                py::handle cls(snapshot_->cls);
                instance_ = py::module_::import("slideio_processor.operation")
                                .attr("_instantiate")(cls, parameterValues(parameters_, *snapshot_));
            } catch (py::error_already_set& e) {
                failure = describe(e);
            }
        }
        if (!failure.empty()) throw std::runtime_error(failure);
        ready_.store(true, std::memory_order_release);
    }

    // Copies the tile region of what apply returned into the output; returns a failure
    // text, or "". Call with the GIL held.
    std::string copyOut(const py::object& returned, int64_t height, int64_t width, int64_t offsetX, int64_t offsetY,
                        sp::BlockView& out) const {
        const std::string label = "operation '" + name_ + "': apply ";
        if (!py::isinstance<py::array>(returned)) return label + "did not return a numpy.ndarray";
        const auto array = py::reinterpret_borrow<py::array>(returned);
        const int32_t planes = snapshot_->outputPlanes;
        const py::dtype expected(numpyName(snapshot_->outputType));
        if (!array.dtype().equal(expected)) return label + "returned the wrong dtype";
        const bool shapeOk = array.ndim() == 3 ? array.shape(2) == planes : array.ndim() == 2 && planes == 1;
        if (!shapeOk || array.shape(0) != height || array.shape(1) != width) return label + "returned the wrong shape";
        if (out.planeCount() != planes || offsetX < 0 || offsetY < 0 || offsetX + out.width() > width ||
            offsetY + out.height() > height) {
            return label + "the output does not fit the returned array";
        }
        const auto size = static_cast<size_t>(sp::dataTypeSizeBytes(snapshot_->outputType));
        const auto* base = static_cast<const char*>(array.data());
        const py::ssize_t planeStride = array.ndim() == 3 ? array.strides(2) : 0;
        for (int32_t p = 0; p < planes; ++p) {
            for (int64_t y = 0; y < out.height(); ++y) {
                char* dst = static_cast<char*>(out.planeData(p)) + y * out.planeRowStrideBytes(p);
                const char* row = base + (y + offsetY) * array.strides(0) + p * planeStride;
                for (int64_t x = 0; x < out.width(); ++x) {
                    std::memcpy(dst + x * size, row + (x + offsetX) * array.strides(1), size);
                }
            }
        }
        return {};
    }

    const std::string name_;
    const SnapshotPtr snapshot_;
    const sp::OperationParameters parameters_;
    std::mutex instanceMutex_;
    std::atomic<bool> ready_{false};
    py::object instance_; // set once under instanceMutex_ and the GIL
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
        return std::make_shared<RasterStandIn>(name_, std::move(s), parameters);
    }

    const char* name() const override { return name_.c_str(); }
    sp::ChannelArity arity() const override { return sp::ChannelArity::Joint; }
    bool requiresUniformTypes() const override { return true; }
    sp::DataTypeSet supportedTypes() const override { return sp::DataTypeSet::all(); }
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

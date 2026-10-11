#define VALUEMACROS_H "$Id$\n"

/* object_compare() is in BTreeModuleTemplate.c. */
#define COMPARE(lhs, rhs) object_compare((lhs), (rhs))

#define VALUE_TYPE PyObject *
#define VALUE_TYPE_IS_PYOBJECT
#define TEST_VALUE(VALUE, TARGET) (COMPARE((VALUE),(TARGET)))
#define DECLARE_VALUE(NAME) VALUE_TYPE NAME
#define INCREF_VALUE(k) Py_INCREF(k)
#define DECREF_VALUE(k) Py_DECREF(k)
#define COPY_VALUE(k,e) k=(e)
#define COPY_VALUE_TO_OBJECT(O, K) O=(K); Py_INCREF(O)
#define COPY_VALUE_FROM_ARG(TARGET, ARG, S) TARGET=(ARG)

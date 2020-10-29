from __future__ import absolute_import, division, print_function, unicode_literals

import re

from stone.backend import CodeBackend
from stone.backends.helpers import fmt_underscores
from stone.backends.python_helpers import (
    check_route_name_conflict,
    fmt_class,
    fmt_func,
    fmt_namespace,
    fmt_obj,
    fmt_type,
    fmt_var,
)
from stone.backends.python_types import (
    class_name_for_data_type,
)
from stone.ir import (
    is_nullable_type,
    is_list_type,
    is_map_type,
    is_struct_type,
    is_tag_ref,
    is_union_type,
    is_user_defined_type,
    is_void_type,
)

_MYPY = False
if _MYPY:
    import typing  # noqa: F401 # pylint: disable=import-error,unused-import,useless-suppression

# Hack to get around some of Python 2's standard library modules that
# accept ascii-encodable unicode literals in lieu of strs, but where
# actually passing such literals results in errors with mypy --py2. See
# <https://github.com/python/typeshed/issues/756> and
# <https://github.com/python/mypy/issues/2536>.
import importlib
argparse = importlib.import_module(str('argparse'))  # type: typing.Any


# This will be at the top of the generated file.
base = """\
# -*- coding: utf-8 -*-
# Auto-generated by Stone, do not modify.
# flake8: noqa
# pylint: skip-file

from abc import ABCMeta, abstractmethod
"""

# Matches format of Babel doc tags
doc_sub_tag_re = re.compile(':(?P<tag>[A-z]*):`(?P<val>.*?)`')

DOCSTRING_CLOSE_RESPONSE = """\
If you do not consume the entire response body, then you must call close on the
response object, otherwise you will max out your available connections. We
recommend using the `contextlib.closing
<https://docs.python.org/2/library/contextlib.html#contextlib.closing>`_
context manager to ensure this."""

_cmdline_parser = argparse.ArgumentParser(
    prog='python-client-backend',
    description=(
        'Generates a Python class with a method for each route. Extend the '
        'generated class and implement the abstract request() method. This '
        'class assumes that the python_types backend was used with the same '
        'output directory.'),
)
_cmdline_parser.add_argument(
    '-m',
    '--module-name',
    required=True,
    type=str,
    help=('The name of the Python module to generate. Please exclude the .py '
          'file extension.'),
)
_cmdline_parser.add_argument(
    '-c',
    '--class-name',
    required=True,
    type=str,
    help='The name of the Python class that contains each route as a method.',
)
_cmdline_parser.add_argument(
    '-t',
    '--types-package',
    required=True,
    type=str,
    help='The output Python package of the python_types backend.',
)
_cmdline_parser.add_argument(
    '-e',
    '--error-class-path',
    default='.exceptions.ApiError',
    type=str,
    help=(
        "The path to the class that's raised when a route returns an error. "
        "The class name is inserted into the doc for route methods."),
)
_cmdline_parser.add_argument(
    '-w',
    '--auth-type',
    type=str,
    help='The auth type of the client to generate.',
)


class PythonClientBackend(CodeBackend):

    cmdline_parser = _cmdline_parser
    supported_auth_types = None

    def generate(self, api):
        """Generates a module called "base".

        The module will contain a base class that will have a method for
        each route across all namespaces.
        """

        with self.output_to_relative_path('%s.py' % self.args.module_name):
            self.emit_raw(base)
            # Import "warnings" if any of the routes are deprecated.
            found_deprecated = False
            for namespace in api.namespaces.values():
                for route in namespace.routes:
                    if route.deprecated:
                        self.emit('import warnings')
                        found_deprecated = True
                        break
                if found_deprecated:
                    break
            self.emit()
            self._generate_imports(api.namespaces.values())
            self.emit()
            self.emit()  # PEP-8 expects two-blank lines before class def
            self.emit('class %s(object):' % self.args.class_name)
            with self.indent():
                self.emit('__metaclass__ = ABCMeta')
                self.emit()
                self.emit('@abstractmethod')
                self.emit(
                    'def request(self, route, namespace, arg, arg_binary=None):')
                with self.indent():
                    self.emit('pass')
                self.emit()
                self._generate_route_methods(api.namespaces.values())

    def _generate_imports(self, namespaces):
        # Only import namespaces that have user-defined types defined.
        for namespace in namespaces:
            if namespace.data_types:
                self.emit('from {} import {}'.format(self.args.types_package, fmt_namespace(namespace.name)))

    def _generate_route_methods(self, namespaces):
        """Creates methods for the routes in each namespace. All data types
        and routes are represented as Python classes."""
        self.cur_namespace = None
        for namespace in namespaces:
            if namespace.routes:
                self.emit('# ------------------------------------------')
                self.emit('# Routes in {} namespace'.format(namespace.name))
                self.emit()
                self._generate_routes(namespace)

    def _generate_routes(self, namespace):
        """
        Generates Python methods that correspond to routes in the namespace.
        """

        # Hack: needed for _docf()
        self.cur_namespace = namespace
        # list of auth_types supported in this base class.
        # this is passed with the new -w flag
        if self.args.auth_type is not None:
            self.supported_auth_types = [auth_type.strip().lower() for auth_type in self.args.auth_type.split(',')]

        check_route_name_conflict(namespace)

        for route in namespace.routes:
            # compatibility mode : included routes are passed by whitelist
            # actual auth attr inluded in the route is ignored in this mode.
            if self.supported_auth_types is None:
                self._generate_route_helper(namespace, route)
                if route.attrs.get('style') == 'download':
                    self._generate_route_helper(namespace, route, True)
            else:
                route_auth_attr = None
                if route.attrs is not None:
                    route_auth_attr = route.attrs.get('auth')
                if route_auth_attr is None:
                    continue
                route_auth_modes = [mode.strip().lower() for mode in route_auth_attr.split(',')]
                for base_auth_type in self.supported_auth_types:
                    if base_auth_type in route_auth_modes:
                        self._generate_route_helper(namespace, route)
                        if route.attrs.get('style') == 'download':
                            self._generate_route_helper(namespace, route, True)
                        break # to avoid duplicate method declaration in the same base class

    def _generate_route_helper(self, namespace, route, download_to_file=False):
        """Generate a Python method that corresponds to a route.

        :param namespace: Namespace that the route belongs to.
        :param stone.ir.ApiRoute route: IR node for the route.
        :param bool download_to_file: Whether a special version of the route
            that downloads the response body to a file should be generated.
            This can only be used for download-style routes.
        """
        arg_data_type = route.arg_data_type
        result_data_type = route.result_data_type

        request_binary_body = route.attrs.get('style') == 'upload'
        response_binary_body = route.attrs.get('style') == 'download'

        if download_to_file:
            assert response_binary_body, 'download_to_file can only be set ' \
                'for download-style routes.'
            self._generate_route_method_decl(namespace,
                                             route,
                                             arg_data_type,
                                             request_binary_body,
                                             method_name_suffix='_to_file',
                                             extra_args=['download_path'])
        else:
            self._generate_route_method_decl(namespace,
                                             route,
                                             arg_data_type,
                                             request_binary_body)

        with self.indent():
            extra_request_args = None
            extra_return_arg = None
            footer = None
            if request_binary_body:
                extra_request_args = [('f',
                                       'bytes',
                                       'Contents to upload.')]
            elif download_to_file:
                extra_request_args = [('download_path',
                                       'str',
                                       'Path on local machine to save file.')]
            if response_binary_body and not download_to_file:
                extra_return_arg = ':class:`requests.models.Response`'
                footer = DOCSTRING_CLOSE_RESPONSE

            if route.doc:
                func_docstring = self.process_doc(route.doc, self._docf)
            else:
                func_docstring = None

            self._generate_docstring_for_func(
                namespace,
                arg_data_type,
                result_data_type,
                route.error_data_type,
                overview=func_docstring,
                extra_request_args=extra_request_args,
                extra_return_arg=extra_return_arg,
                footer=footer,
            )

            self._maybe_generate_deprecation_warning(route)

            # Code to instantiate a class for the request data type
            if is_void_type(arg_data_type):
                self.emit('arg = None')
            elif is_struct_type(arg_data_type):
                self.generate_multiline_list(
                    [f.name for f in arg_data_type.all_fields],
                    before='arg = {}.{}'.format(
                        fmt_namespace(arg_data_type.namespace.name),
                        fmt_class(arg_data_type.name)),
                )
            elif not is_union_type(arg_data_type):
                raise AssertionError('Unhandled request type %r' %
                                     arg_data_type)

            # Code to make the request
            args = [
                '{}.{}'.format(fmt_namespace(namespace.name),
                               fmt_func(route.name, version=route.version)),
                "'{}'".format(namespace.name),
                'arg']
            if request_binary_body:
                args.append('f')
            else:
                args.append('None')
            self.generate_multiline_list(args, 'r = self.request', compact=False)

            if download_to_file:
                self.emit('self._save_body_to_file(download_path, r[1])')
                if is_void_type(result_data_type):
                    self.emit('return None')
                else:
                    self.emit('return r[0]')
            else:
                if is_void_type(result_data_type):
                    self.emit('return None')
                else:
                    self.emit('return r')
        self.emit()

    def _generate_route_method_decl(
            self, namespace, route, arg_data_type, request_binary_body,
            method_name_suffix='', extra_args=None):
        """Generates the method prototype for a route."""
        args = ['self']
        if extra_args:
            args += extra_args
        if request_binary_body:
            args.append('f')
        if is_struct_type(arg_data_type):
            for field in arg_data_type.all_fields:
                if is_nullable_type(field.data_type):
                    args.append('{}=None'.format(field.name))
                elif field.has_default:
                    # TODO(kelkabany): Decide whether we really want to set the
                    # default in the argument list. This will send the default
                    # over the wire even if it isn't overridden. The benefit is
                    # it locks in a default even if it is changed server-side.
                    if is_user_defined_type(field.data_type):
                        ns = field.data_type.namespace
                    else:
                        ns = None
                    arg = '{}={}'.format(
                        field.name,
                        self._generate_python_value(ns, field.default))
                    args.append(arg)
                else:
                    args.append(field.name)
        elif is_union_type(arg_data_type):
            args.append('arg')
        elif not is_void_type(arg_data_type):
            raise AssertionError('Unhandled request type: %r' %
                                 arg_data_type)

        method_name = fmt_func(route.name + method_name_suffix, version=route.version)
        namespace_name = fmt_underscores(namespace.name)
        self.generate_multiline_list(args, 'def {}_{}'.format(namespace_name, method_name), ':')

    def _maybe_generate_deprecation_warning(self, route):
        if route.deprecated:
            msg = '{} is deprecated.'.format(route.name)
            if route.deprecated.by:
                msg += ' Use {}.'.format(route.deprecated.by.name)
            args = ["'{}'".format(msg), 'DeprecationWarning']
            self.generate_multiline_list(
                args,
                before='warnings.warn',
                delim=('(', ')'),
                compact=False,
            )

    def _generate_docstring_for_func(self, namespace, arg_data_type,
                                     result_data_type=None, error_data_type=None,
                                     overview=None, extra_request_args=None,
                                     extra_return_arg=None, footer=None):
        """
        Generates a docstring for a function or method.

        This function is versatile. It will create a docstring using all the
        data that is provided.

        :param arg_data_type: The data type describing the argument to the
            route. The data type should be a struct, and each field will be
            treated as an input parameter of the method.
        :param result_data_type: The data type of the route result.
        :param error_data_type: The data type of the route result in the case
            of an error.
        :param str overview: A description of the route that will be located
            at the top of the docstring.
        :param extra_request_args: [(field name, field type, field doc), ...]
            Describes any additional parameters for the method that aren't a
            field in arg_data_type.
        :param str extra_return_arg: Name of an additional return type that. If
            this is specified, it is assumed that the return of the function
            will be a tuple of return_data_type and extra_return-arg.
        :param str footer: Additional notes at the end of the docstring.
        """
        fields = [] if is_void_type(arg_data_type) else arg_data_type.fields
        if not fields and not overview:
            # If we don't have an overview or any input parameters, we skip the
            # docstring altogether.
            return

        self.emit('"""')
        if overview:
            self.emit_wrapped_text(overview)

        # Description of all input parameters
        if extra_request_args or fields:
            if overview:
                # Add a blank line if we had an overview
                self.emit()

            if extra_request_args:
                for name, data_type_name, doc in extra_request_args:
                    if data_type_name:
                        field_doc = ':param {} {}: {}'.format(data_type_name,
                                                              name, doc)
                        self.emit_wrapped_text(field_doc,
                                               subsequent_prefix='    ')
                    else:
                        self.emit_wrapped_text(
                            ':param {}: {}'.format(name, doc),
                            subsequent_prefix='    ')

            if is_struct_type(arg_data_type):
                for field in fields:
                    if field.doc:
                        if is_user_defined_type(field.data_type):
                            field_doc = ':param {}: {}'.format(
                                field.name, self.process_doc(field.doc, self._docf))
                        else:
                            field_doc = ':param {} {}: {}'.format(
                                self._format_type_in_doc(namespace, field.data_type),
                                field.name,
                                self.process_doc(field.doc, self._docf),
                            )
                        self.emit_wrapped_text(
                            field_doc, subsequent_prefix='    ')
                        if is_user_defined_type(field.data_type):
                            # It's clearer to declare the type of a composite on
                            # a separate line since it references a class in
                            # another module
                            self.emit(':type {}: {}'.format(
                                field.name,
                                self._format_type_in_doc(namespace, field.data_type),
                            ))
                    else:
                        # If the field has no docstring, then just document its
                        # type.
                        field_doc = ':type {}: {}'.format(
                            field.name,
                            self._format_type_in_doc(namespace, field.data_type),
                        )
                        self.emit_wrapped_text(field_doc)

            elif is_union_type(arg_data_type):
                if arg_data_type.doc:
                    self.emit_wrapped_text(':param arg: {}'.format(
                        self.process_doc(arg_data_type.doc, self._docf)),
                        subsequent_prefix='    ')
                self.emit(':type arg: {}'.format(
                    self._format_type_in_doc(namespace, arg_data_type)))

        if overview and not (extra_request_args or fields):
            # Only output an empty line if we had an overview and haven't
            # started a section on declaring types.
            self.emit()

        if extra_return_arg:
            # Special case where the function returns a tuple. The first
            # element is the JSON response. The second element is the
            # the extra_return_arg param.
            args = []
            if is_void_type(result_data_type):
                args.append('None')
            else:
                rtype = self._format_type_in_doc(namespace,
                                                 result_data_type)
                args.append(rtype)
            args.append(extra_return_arg)
            self.generate_multiline_list(args, ':rtype: ')
        else:
            if is_void_type(result_data_type):
                self.emit(':rtype: None')
            else:
                rtype = self._format_type_in_doc(namespace, result_data_type)
                self.emit(':rtype: {}'.format(rtype))

        if not is_void_type(error_data_type) and error_data_type.fields:
            self.emit(':raises: :class:`{}`'.format(self.args.error_class_path))
            self.emit()
            # To provide more clarity to a dev who reads the docstring, suggest
            # the route's error class. This is confusing, however, because we
            # don't know where the error object that's raised will store
            # the more detailed route error defined in stone.
            error_class_name = self.args.error_class_path.rsplit('.', 1)[-1]
            self.emit('If this raises, {} will contain:'.format(error_class_name))
            with self.indent():
                self.emit(self._format_type_in_doc(namespace, error_data_type))

        if footer:
            self.emit()
            self.emit_wrapped_text(footer)
        self.emit('"""')

    def _docf(self, tag, val):
        """
        Callback used as the handler argument to process_docs(). This converts
        Babel doc references to Sphinx-friendly annotations.
        """
        if tag == 'type':
            fq_val = val
            if '.' not in val:
                fq_val = self.cur_namespace.name + '.' + fq_val
            return ':class:`{}.{}`'.format(self.args.types_package, fq_val)
        elif tag == 'route':
            if ':' in val:
                val, version = val.split(':', 1)
                version = int(version)
            else:
                version = 1
            if '.' in val:
                return ':meth:`{}`'.format(fmt_func(val, version=version))
            else:
                return ':meth:`{}_{}`'.format(
                    self.cur_namespace.name, fmt_func(val, version=version))
        elif tag == 'link':
            anchor, link = val.rsplit(' ', 1)
            return '`{} <{}>`_'.format(anchor, link)
        elif tag == 'val':
            if val == 'null':
                return 'None'
            elif val == 'true' or val == 'false':
                return '``{}``'.format(val.capitalize())
            else:
                return val
        elif tag == 'field':
            return '``{}``'.format(val)
        else:
            raise RuntimeError('Unknown doc ref tag %r' % tag)

    def _format_type_in_doc(self, namespace, data_type):
        """
        Returns a string that can be recognized by Sphinx as a type reference
        in a docstring.
        """
        if is_void_type(data_type):
            return 'None'
        elif is_user_defined_type(data_type):
            return ':class:`{}.{}.{}`'.format(
                self.args.types_package, namespace.name, fmt_type(data_type))
        elif is_nullable_type(data_type):
            return 'Nullable[{}]'.format(
                self._format_type_in_doc(namespace, data_type.data_type),
            )
        elif is_list_type(data_type):
            return 'List[{}]'.format(
                self._format_type_in_doc(namespace, data_type.data_type),
            )
        elif is_map_type(data_type):
            return 'Map[{}, {}]'.format(
                self._format_type_in_doc(namespace, data_type.key_data_type),
                self._format_type_in_doc(namespace, data_type.value_data_type),
            )
        else:
            return fmt_type(data_type)

    def _generate_python_value(self, namespace, value):
        if is_tag_ref(value):
            return '{}.{}.{}'.format(
                fmt_namespace(namespace.name),
                class_name_for_data_type(value.union_data_type),
                fmt_var(value.tag_name))
        else:
            return fmt_obj(value)

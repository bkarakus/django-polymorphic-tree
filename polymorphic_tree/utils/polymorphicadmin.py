"""
ModelAdmin code to display polymorphic models.
"""
from django import forms
from django.conf.urls.defaults import patterns, url
from django.contrib import admin
from django.contrib.admin.helpers import AdminForm, AdminErrorList
from django.contrib.admin.sites import AdminSite
from django.contrib.admin.widgets import AdminRadioSelect
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import RegexURLResolver
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render_to_response
from django.template.context import RequestContext
from django.utils.datastructures import SortedDict
from django.utils.encoding import force_unicode
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
import abc

__all__ = ('PolymorphicModelChoiceForm', 'PolymorphicParentModelAdmin', 'PolymorphicChildModelAdmin')


class PolymorphicModelChoiceForm(forms.Form):
    """
    The default form for the ``add_type_form``. Can be overwritten and replaced.
    """
    ct_id = forms.ChoiceField(label=_("Type"), widget=AdminRadioSelect(attrs={'class': 'radiolist'}))



class PolymorphicParentModelAdmin(admin.ModelAdmin):
    """
    A admin interface that can displays different change/delete pages, depending on the polymorphic model.
    To use this class, two variables need to be defined:

    * :attr:`base_model` should
    * :attr:`child_models` should be a list of (Model, Admin) tuples

    Alternatively, the following methods can be implemented:

    * :func:`get_admin_for_model` should return a ModelAdmin instance for the derived model.
    * :func:`get_child_model_classes` should return the available derived models.
    * optionally, :func:`get_child_type_choices` can be overwritten to refine the choices for the add dialog.

    This class needs to be inherited by the model admin base class that is registered in the site.
    The derived models should *not* register the ModelAdmin, but instead it should be returned by :func:`get_admin_for_model`.
    """

    #: The base model that the class uses
    base_model = None

    #: The child models that should be displayed
    child_models = None

    #: Whether the list should be polymorphic too, leave to ``False`` to optimize
    polymorphic_list = False

    add_type_template = None
    add_type_form = PolymorphicModelChoiceForm


    def __init__(self, model, admin_site, *args, **kwargs):
        super(PolymorphicParentModelAdmin, self).__init__(model, admin_site, *args, **kwargs)
        self.initialized_child_models = None
        self.child_admin_site = AdminSite(name='polymorphic_child_admin')

        # Allow to declaratively define the child models + admin classes
        if self.child_models is not None:
            self.initialized_child_models = SortedDict()
            for Model, Admin in self.child_models:
                assert issubclass(Model, self.base_model), "{0} should be a subclass of {1}".format(Model.__name__, self.base_model.__name__)
                assert issubclass(Admin, admin.ModelAdmin), "{0} should be a subclass of {1}".format(Admin.__name__, admin.ModelAdmin.__name__)
                self.child_admin_site.register(Model, Admin)

                # HACK: need to get admin instance.
                admin_instance = self.child_admin_site._registry[Model]
                self.initialized_child_models[Model] = admin_instance


    @abc.abstractmethod
    def get_admin_for_model(self, model):
        """
        Return the polymorphic admin interface for a given model.
        """
        if self.initialized_child_models is None:
            raise NotImplementedError("Implement get_admin_for_model() or child_models")

        return self.initialized_child_models[model]


    @abc.abstractmethod
    def get_child_model_classes(self):
        """
        Return the derived model classes which this admin should handle.

        This could either be implemented as ``base_model.__subclasses__()``,
        a setting in a config file, or a query of a plugin registration system.
        """
        if self.initialized_child_models is None:
            raise NotImplementedError("Implement get_child_model_classes() or child_models")

        return self.initialized_child_models.keys()


    def get_child_type_choices(self):
        """
        Return a list of polymorphic types which can be added.
        """
        choices = []
        for model in self.get_child_model_classes():
            ct = ContentType.objects.get_for_model(model)
            choices.append((ct.id, model._meta.verbose_name))
        return choices


    def _get_real_admin(self, object_id):
        obj = self.model.objects.non_polymorphic().values('polymorphic_ctype').get(pk=object_id)
        return self._get_real_admin_by_ct(obj['polymorphic_ctype'])


    def _get_real_admin_by_ct(self, ct_id):
        try:
            ct = ContentType.objects.get_for_id(ct_id)
        except ContentType.DoesNotExist as e:
            raise Http404(e)   # Handle invalid GET parameters

        model_class = ct.model_class()
        if not model_class:
            raise Http404("No model found for '{0}.{1}'.".format(*ct.natural_key()))  # Handle model deletion

        # The views are already checked for permissions, so ensure the model is a derived object.
        # Otherwise, it would open all admin views to users who can edit the base object.
        if not issubclass(model_class, self.base_model):
            raise PermissionDenied("Invalid model '{0}.{1}', must derive from {name}.".format(*ct.natural_key(), name=self.base_model.__name__))

        return self.get_admin_for_model(model_class)


    def queryset(self, request):
        # optimize the list display.
        qs = super(PolymorphicParentModelAdmin, self).queryset(request)
        if not self.polymorphic_list:
            qs = qs.non_polymorphic()
        return qs


    def add_view(self, request, form_url='', extra_context=None):
        """Redirect the add view to the real admin."""
        ct_id = int(request.GET.get('ct_id', 0))
        if not ct_id:
            # Display choices
            return self.add_type_view(request)
        else:
            real_admin = self._get_real_admin_by_ct(ct_id)
            return real_admin.add_view(request, form_url, extra_context)


    def change_view(self, request, object_id, *args, **kwargs):
        """Redirect the change view to the real admin."""
        # between Django 1.3 and 1.4 this method signature differs. Hence the *args, **kwargs
        real_admin = self._get_real_admin(object_id)
        return real_admin.change_view(request, object_id, *args, **kwargs)


    def delete_view(self, request, object_id, extra_context=None):
        """Redirect the delete view to the real admin."""
        real_admin = self._get_real_admin(object_id)
        return real_admin.delete_view(request, object_id, extra_context)


    def get_urls(self):
        """
        Expose the custom URLs for the subclasses and the URL resolver.
        """
        urls = super(PolymorphicParentModelAdmin, self).get_urls()
        info = self.model._meta.app_label, self.model._meta.module_name

        # Patch the change URL so it's not a big catch-all; allowing all custom URLs to be added to the end.
        # The url needs to be recreated, patching url.regex is not an option Django 1.4's LocaleRegexProvider changed it.
        new_change_url = url(r'^(\d+)/$', self.admin_site.admin_view(self.change_view), name='{0}_{1}_change'.format(*info))
        for i, oldurl in enumerate(urls):
            if oldurl.name == new_change_url.name:
                urls[i] = new_change_url

        # Define the catch-all for custom views
        custom_urls = patterns('',
            url(r'^(?P<path>.+)$', self.admin_site.admin_view(self.subclass_view))
        )

        # Add reverse names for all polymorphic models, so the delete button and "save and add" just work.
        # These definitions are masked by the definition above, since it needs special handling (and a ct_id parameter).
        dummy_urls = []
        for model in self.get_child_model_classes():
            admin = self.get_admin_for_model(model)
            dummy_urls += admin.get_urls()

        return urls + custom_urls + dummy_urls


    def subclass_view(self, request, path):
        """
        Forward any request to a custom view of the real admin.
        """
        ct_id = int(request.GET.get('ct_id', 0))
        if not ct_id:
            raise Http404("No ct_id parameter, unable to find admin subclass for path '{0}'.".format(path))

        real_admin = self._get_real_admin_by_ct(ct_id)
        resolver = RegexURLResolver('^', real_admin.urls)
        resolvermatch = resolver.resolve(path)
        if not resolvermatch:
            raise Http404("No match for path '{0}' in admin subclass.".format(path))

        return resolvermatch.func(request, *resolvermatch.args, **resolvermatch.kwargs)


    def add_type_view(self, request, form_url=''):
        """
        Display a choice form to select which page type to add.
        """
        extra_qs = ''
        if request.META['QUERY_STRING']:
            extra_qs = '&' + request.META['QUERY_STRING']

        choices = self.get_child_type_choices()
        if len(choices) == 1:
            return HttpResponseRedirect('?ct_id={0}{1}'.format(choices[0][0], extra_qs))

        # Create form
        form = self.add_type_form(
            data=request.POST if request.method == 'POST' else None,
            initial={'ct_id': choices[0][0]}
        )
        form.fields['ct_id'].choices = choices

        if form.is_valid():
            return HttpResponseRedirect('?ct_id={0}{1}'.format(form.cleaned_data['ct_id'], extra_qs))

        # Wrap in all admin layout
        fieldsets = ((None, {'fields': ('ct_id',)}),)
        adminForm = AdminForm(form, fieldsets, {}, model_admin=self)
        media = self.media + adminForm.media
        opts = self.model._meta

        context = {
            'title': _('Add %s') % force_unicode(opts.verbose_name),
            'adminform': adminForm,
            'is_popup': "_popup" in request.REQUEST,
            'media': mark_safe(media),
            'errors': AdminErrorList(form, ()),
            'app_label': opts.app_label,
        }
        return self.render_add_type_form(request, context, form_url)


    def render_add_type_form(self, request, context, form_url=''):
        """
        Render the page type choice form.
        """
        opts = self.model._meta
        app_label = opts.app_label
        context.update({
            'has_change_permission': self.has_change_permission(request),
            'form_url': mark_safe(form_url),
            'opts': opts,
        })
        if hasattr(self.admin_site, 'root_path'):
            context['root_path'] = self.admin_site.root_path  # Django < 1.4
        context_instance = RequestContext(request, current_app=self.admin_site.name)
        return render_to_response(self.add_type_template or [
            "admin/%s/%s/add_type_form.html" % (app_label, opts.object_name.lower()),
            "admin/%s/add_type_form.html" % app_label,
            "admin/polymorphic_tree/add_type_form.html",  # NOTE: added
            "admin/add_type_form.html"
        ], context, context_instance=context_instance)


    @property
    def change_list_template(self):
        opts = self.model._meta
        app_label = opts.app_label

        # Pass the base options
        base_opts = self.base_model._meta
        base_app_label = base_opts.app_label

        return [
            "admin/%s/%s/change_list.html" % (app_label, opts.object_name.lower()),
            "admin/%s/change_list.html" % app_label,
            # Added:
            "admin/%s/%s/change_list.html" % (base_app_label, base_opts.object_name.lower()),
            "admin/%s/change_list.html" % base_app_label,
            "admin/polymorphic_tree/nodetype/change_list.html",  # NOTE: added
            "admin/change_list.html"
        ]



class PolymorphicChildModelAdmin(admin.ModelAdmin):
    """
    The *optional* base class for the admin interface of derived models.

    This base class defines some convenience behavior for the admin interface:

    * It corrects the breadcrumbs in the admin pages.
    * It adds the base model to the template lookup paths.
    * It allows to set ``base_form`` so the derived class will automatically include other fields in the form.
    * It allows to set ``base_fieldsets`` so the derived class will automatically display any extra fields.

    The ``base_model`` attribute must be set.
    """
    base_model = None
    base_form = None
    base_fieldsets = None
    extra_fieldset_title = _("Contents")  # Default title for extra fieldset


    def get_form(self, request, obj=None, **kwargs):
        # The django admin validation requires the form to have a 'class Meta: model = ..'
        # attribute, or it will complain that the fields are missing.
        # However, this enforces all derived ModelAdmin classes to redefine the model as well,
        # because they need to explicitly set the model again - it will stick with the base model.
        #
        # Instead, pass the form unchecked here, because the standard ModelForm will just work.
        # If the derived class sets the model explicitly, respect that setting.
        if not self.form:
            kwargs['form'] = self.base_form
        return super(PolymorphicChildModelAdmin, self).get_form(request, obj, **kwargs)


    @property
    def change_form_template(self):
        opts = self.model._meta
        app_label = opts.app_label

        # Pass the base options
        base_opts = self.base_model._meta
        base_app_label = base_opts.app_label

        return [
            "admin/%s/%s/change_form.html" % (app_label, opts.object_name.lower()),
            "admin/%s/change_form.html" % app_label,
            # Added:
            "admin/%s/%s/change_form.html" % (base_app_label, base_opts.object_name.lower()),
            "admin/%s/change_form.html" % base_app_label,
            "admin/polymorphic_tree/nodetype/change_form.html",  # NOTE: added
            "admin/change_form.html"
        ]


    @property
    def delete_confirmation_template(self):
        opts = self.model._meta
        app_label = opts.app_label

        # Pass the base options
        base_opts = self.base_model._meta
        base_app_label = base_opts.app_label

        return [
            "admin/%s/%s/delete_confirmation.html" % (app_label, opts.object_name.lower()),
            "admin/%s/delete_confirmation.html" % app_label,
            # Added:
            "admin/%s/%s/delete_confirmation.html" % (base_app_label, base_opts.object_name.lower()),
            "admin/%s/delete_confirmation.html" % base_app_label,
            "admin/polymorphic_tree/nodetype/delete_confirmation.html",  # NOTE: added
            "admin/delete_confirmation.html"
        ]


    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        context.update({
            'base_opts': self.base_model._meta,
        })
        return super(PolymorphicChildModelAdmin, self).render_change_form(request, context, add=add, change=change, form_url=form_url, obj=obj)


    def delete_view(self, request, object_id, context=None):
        extra_context = {
            'base_opts': self.base_model._meta,
        }
        return super(PolymorphicChildModelAdmin, self).delete_view(request, object_id, extra_context)


    # ---- Extra: improving the form/fieldset default display ----

    def get_fieldsets(self, request, obj=None):
        # If subclass declares fieldsets, this is respected
        if self.declared_fieldsets or not self.base_fieldsets:
            return super(PolymorphicChildModelAdmin, self).get_fieldsets(request, obj)

        # Have a reasonable default fieldsets,
        # where the subclass fields are automatically included.
        other_fields = self.get_subclass_fields(request, obj)

        if other_fields:
            return (
                self.base_fieldsets[0],
                (self.extra_fieldset_title, {'fields': other_fields}),
            ) + self.base_fieldsets[1:]
        else:
            return self.base_fieldsets


    def get_subclass_fields(self, request, obj=None):
        # Find out how many fields would really be on the form,
        # if it weren't restricted by declared fields.
        exclude = list(self.exclude or [])
        exclude.extend(self.get_readonly_fields(request, obj))

        # By not declaring the fields/form in the base class,
        # get_form() will populate the form with all available fields.
        form = self.get_form(request, obj, exclude=exclude)
        subclass_fields = form.base_fields.keys() + list(self.get_readonly_fields(request, obj))

        # Find which fields are not part of the common fields.
        for fieldset in self.base_fieldsets:
            for field in fieldset[1]['fields']:
                try:
                    subclass_fields.remove(field)
                except ValueError:
                    pass   # field not found in form, Django will raise exception later.
        return subclass_fields

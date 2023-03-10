import warnings
from itertools import chain
from typing import TYPE_CHECKING

from fireo.database import db
import fireo.fields as fields
from fireo.fields.errors import RequiredField
from fireo.managers.managers import Manager
from fireo.models.errors import AbstractNotInstantiate, ModelSerializingWrappedError
from fireo.models.model_meta import ModelMeta
from fireo.queries.errors import InvalidKey
from fireo.utils import utils
from fireo.utils.types import DumpOptions, LoadOptions

if TYPE_CHECKING:
    from fireo.fields import Field


class Model(metaclass=ModelMeta):
    """Provide easy way to handle firestore features

    Model is used to handle firestore operation easily and provide additional features for best
    user experience.

    Example
    -------
    .. code-block:: python

        class User(Model):
            username = TextField(required=True)
            full_name = TextField()
            age = NumberField()

        user = User()
        user.username = "Axeem"
        user.full_name = "Azeem Haider"
        user.age = 25
        user.save()

        # or you can also pass args into constructor
        user = User(username="Axeem", full_name="Azeem", age=25)
        user.save()

        # or you can use it via managers
        user = User.collection.create(username="Axeem", full_name="Azeem", age=25)

    Attributes
    ----------
    _meta : Meta
        Hold all model information like model fields, id, manager etc

    id : str
        Model id if user specify any otherwise it will create automatically from firestore
        and attached with this model

    key : str
        Model key which contain the model collection name and model id and parent if provided, Id can be user defined
        or generated from firestore

    parent: str
        Parent key if user specify

    collection_name : str
        Model name which is saved in firestore if user not specify any then Model class will convert
        automatically in collection name

        For example: UserProfile will be user_profile

    collection : Manager
        Class level attribute through this you can access manager which can be used to save, retrieve or
        update the model in firestore

        Example:
        -------
        .. code-block:: python
            class User(Model):
                name = TextField()

            user = User.collection.create(name="Azeem")

    Methods
    --------
    _get_fields() : dict
        Private method that return values of all attached fields.

    save() : Model instance
        Save the model in firestore collection

    update(doc_key, transaction) : Model instance
        Update the existing document, doc_key optional can be set explicitly

    _set_key(doc_id):
        Set the model key

    Raises
    ------
    AbstractNotInstantiate:
        Abstract model can not instantiate
    """
    # Id of the model specify by user or auto generated by firestore
    # It can be None if user changed the name of id field it is possible
    # to call it from different name e.g user_id
    id = None

    # Private for internal user but there is key property which hold the
    # current document id and collection name and parent if any
    _key = None

    # For sub collection there must be a parent
    parent = ""

    # Hold all the information about the model fields
    _meta = None

    # This is for manager
    collection: Manager = None

    # Collection name for this model
    collection_name = None

    # Track which fields are changed or not
    # it is useful when updating document
    _field_changed = None

    _create_time = None
    _update_time = None

    class Meta:
        abstract = True

    def __init__(self, *args, parent: str = "", **kwargs):
        self.parent = parent
        if args:
            raise AttributeError('You must use keyword arguments when instantiating a model')
        unexpected_kwargs = set(kwargs) - set(self._meta.field_list)
        if unexpected_kwargs:
            raise AttributeError(
                'You passed in unknown keyword arguments: {}'.format(', '.join(unexpected_kwargs))
            )

        # check this is not abstract model otherwise stop creating instance of this model
        if self._meta.abstract:
            raise AbstractNotInstantiate(
                f'Can not instantiate abstract model "{self.__class__.__name__}"')

        self._field_changed = set()
        self._extra_fields = set()

        # Allow users to set fields values direct from the constructor method
        for k, v in kwargs.items():
            setattr(self, k, v)

        # Create instance for nested model
        # for direct assignment to nested model
        for f in self._meta.field_list.values():
            if isinstance(f, fields.NestedModelField):
                if f.name not in kwargs:
                    setattr(self, f.name, f.nested_model())
                elif isinstance(kwargs[f.name], dict):
                    warnings.warn(
                        'Use Model.from_dict to deserialize from dict',
                        DeprecationWarning
                    )
                    setattr(self, f.name, f.nested_model.from_dict(kwargs[f.name]))

    @classmethod
    def from_dict(cls, model_dict, by_column_name=False):
        """Instantiate model from dict"""
        if model_dict is None:
            return None

        instance = cls()
        instance.populate_from_doc_dict(model_dict, by_column_name=by_column_name)
        return instance

    def merge_with_dict(self, model_dict, by_column_name=False):
        """Load data from dict into model."""
        self.populate_from_doc_dict(model_dict, merge=True, by_column_name=by_column_name)

    def to_dict(self):
        """Convert model into dict"""
        model_dict = self.to_db_dict()
        id_field_name, _ = self._meta.id
        model_dict[id_field_name] = utils.get_id(self.key)
        model_dict['key'] = self.key
        return model_dict

    def to_db_dict(self, dump_options=DumpOptions()):
        from fireo.fields import IDField

        result = {}
        for field in self._meta.field_list.values():
            field: Field  # type: ignore

            if isinstance(field, IDField):
                if not field.include_in_document:
                    # do not include ID field to dict for firestore unless it is explicitly set
                    continue

            field_changed = self._is_field_unchanged(field.name)
            if dump_options.ignore_unchanged and not field_changed:
                continue

            try:
                nested_field_value = getattr(self, field.name)
                value = field.get_value(nested_field_value, dump_options)
            except Exception as error:
                path = (field.name,)
                raise ModelSerializingWrappedError(self, path, error) from error

            if (
                value is not None or
                not dump_options.ignore_default_none or
                field_changed
            ):
                result[field.db_column_name] = value

        return result

    def populate_from_doc_dict(self, doc_dict: dict, stored=False, merge=False, by_column_name=False):
        """Populate model from Firestore document dict."""
        if not merge:
            old_extra_fields = set(self._extra_fields) - set(self._meta.field_list)
            for extra_field in old_extra_fields:
                delattr(self, extra_field)
            self._extra_fields = set()

        new_extra_fields_names = set(doc_dict) - set(self._meta.field_list)
        if new_extra_fields_names and not by_column_name:
            raise NotImplementedError(
                f"Can't populate model from dict with unknown fields: {new_extra_fields_names}"
            )

        new_extra_fields = [
            self._meta.get_field_by_column_name(field_name)
            for field_name in new_extra_fields_names
            # get_field_by_column_name returns None if extra fields are ignored
            if self._meta.get_field_by_column_name(field_name) is not None
        ]

        for field in chain(self._meta.field_list.values(), new_extra_fields):
            field_name_in_dict = field.db_column_name if by_column_name else field.name
            raw_value = None
            if field_name_in_dict in doc_dict:
                raw_value = doc_dict[field_name_in_dict]

            has_value = getattr(self, field.name, None) is not None
            if field_name_in_dict in doc_dict or (not merge and has_value):
                # Set value from doc_dict
                # or reset value if merge is False and field has value
                value = field.field_value(raw_value, LoadOptions(
                    model=self,
                    stored=stored,
                    merge=merge,
                    by_column_name=by_column_name,
                ))

                if stored:
                    self._set_orig_attr(field.name, value)
                else:
                    setattr(self, field.name, value)

        if not merge and stored:
            # If all fields where replaced by stored values
            # then there are no changed fields
            self._field_changed = set()

    # Get all the fields values from meta
    # which are attached with this mode
    # to create or update the document
    # return dict {name: value}
    def _get_fields(self, ignore_unchanged=False, ignore_default_none=False):
        """Get Model fields and values

        Retrieve all fields which are attached with Model from `_meta`
        then get corresponding value from model

        Example
        -------
        .. code-block:: python

            class User(Model):
                name = TextField()
                age = NumberField()

            user = User()
            user.name = "Azeem"
            user.age = 25

            # if you call this method `_get_field()` it will return dict{name, val}
            # in this case it will be
            {name: "Azeem", age: 25}

        Returns
        -------
        dict:
            name value dict of model
        """
        field_list = {}
        for f in self._meta.field_list.values():
            v = getattr(self, f.name)
            field_changed = self._is_field_unchanged(f.name)
            if (
                (not ignore_unchanged or field_changed) and
                (not ignore_default_none or field_changed or v is not None)
            ):
                field_list[f.name] = v
        return field_list

    @property
    def _id(self):
        """Get Model id

        User can specify model id otherwise it will return None and generate later from
        firestore and attached to model

        Example
        --------
        .. code-block:: python
            class User(Mode):
                user_id = IDField()

            u = User()
            u.user_id = "custom_doc_id"

            # If you call this property it will return user defined id in this case
            print(self._id)  # custom_doc_id

        Returns
        -------
        id : str or None
            User defined id or None
        """
        name, field = self._meta.id
        raw_value = getattr(self, name)
        value = field.get_value(raw_value)
        if raw_value is None and value is not None:
            setattr(self, name, value)

        return value

    @_id.setter
    def _id(self, doc_id):
        """Set Model id

        Set user defined id to model otherwise auto generate from firestore and attach
        it to with model

        Example:
        --------
            class User(Model):
                user_id = IDField()
                name = TextField()

            u = User()
            u.name = "Azeem"
            u.save()

            # User not specify any id it will auto generate from firestore
            print(u.user_id)  # xJuythTsfLs

        Parameters
        ----------
        doc_id : str
            Id of the model user specified or auto generated from firestore
        """
        id_field_name, _ = self._meta.id
        setattr(self, id_field_name, doc_id)
        # Doc id can be None when user create Model directly from manager
        # For Example:
        #   User.collection.create(name="Azeem")
        # in this any empty doc id send just for setup things
        if doc_id:
            self._set_key(doc_id)

    @property
    def key(self):
        if self._key:
            return self._key
        try:
            k = '/'.join([self.parent, self.collection_name, self._id])
        except (TypeError, RequiredField):
            k = '/'.join([self.parent, self.collection_name, '@temp_doc_id'])
        if k[0] == '/':
            return k[1:]
        else:
            return k

    def _set_key(self, doc_id):
        """Set key for model"""
        p = '/'.join([self.parent, self.collection_name, doc_id])
        if p[0] == '/':
            self._key = p[1:]
        else:
            self._key = p

    def get_firestore_create_time(self):
        """returns create time of document in Firestore

        Returns:
            :class:`google.api_core.datetime_helpers.DatetimeWithNanoseconds`,
            :class:`datetime.datetime` or ``NoneType``:
        """
        return self._create_time

    def get_firestore_update_time(self):
        """returns update time of document in Firestore

        Returns:
            :class:`google.api_core.datetime_helpers.DatetimeWithNanoseconds`,
            :class:`datetime.datetime` or ``NoneType``:
        """
        return self._update_time

    def list_subcollections(self):
        """return a list of any subcollections of the doc"""
        return [c.id for c in self.document_reference().collections()]

    def save(self, transaction=None, batch=None, merge=None, no_return=False):
        """Save Model in firestore collection

        Model classes can saved in firestore using this method

        Example
        -------
        .. code-block:: python
            class User(Model):
                name = TextField()
                age = NumberField()

            u = User(name="Azeem", age=25)
            u.save()

            # print model id
            print(u.id) #  xJuythTsfLs

        Same thing can be achieved from using managers

        See Also
        --------
        fireo.managers.Manager()

        Returns
        -------
        model instance:
            Modified instance of the model contains id etc
        """
        # pass the model instance if want change in it after save, fetch etc operations
        # otherwise it will return new model instance
        return self.__class__.collection.create(
            self,
            transaction,
            batch,
            merge,
            no_return,
            **self._get_fields(ignore_default_none=True)
        )

    def upsert(self, transaction=None, batch=None):
        """If the document does not exist, it will be created. 
        If the document does exist it should be merged into the existing document.
        """
        return self.save(transaction=transaction, batch=batch, merge=True)

    def update(self, key=None, transaction=None, batch=None):
        """Update the existing document

        Update document without overriding it. You can update selected fields.

        Examples
        --------
        .. code-block:: python
            class User(Model):
                name = TextField()
                age = NumberField()

            u = User.collection.create(name="Azeem", age=25)
            id = u.id

            # update this
            user = User.collection.get(id)
            user.name = "Arfan"
            user.update()

            print(user.name)  # Arfan
            print(user.age)  # 25

        Parameters
        ----------
        key: str
            Key of document which is going to update this is optional you can also set
            the update_doc explicitly

        transaction:
            Firestore transaction

        batch:
            Firestore batch writes
        """

        # Check doc key is given or not
        if not key:
            key = self.key

        # make sure update doc in not None
        if key is not None and '@temp_doc_id' not in key:
            # set parent doc from this updated document key
            self.parent = utils.get_parent_doc(key)
            # Get id from key and set it for model
            self._id = utils.get_id(key)
        elif key is None and '@temp_doc_id' in self.key:
            raise InvalidKey(
                f'Invalid key to update model "{self.__class__.__name__}" ')

        # pass the model instance if want change in it after save, fetch etc operations
        # otherwise it will return new model instance
        return self.__class__.collection._update(self, transaction=transaction, batch=batch)

    def refresh(self, transaction=None):
        """Refresh the model from firestore"""
        if self.key is None:
            raise ValueError('Model must have key to refresh')

        return self.__class__.collection._refresh(self, transaction=transaction)

    def __setattr__(self, key, value):
        """Keep track which filed values are changed"""
        if key in self._meta.field_list:
            self._field_changed.add(key)
        super(Model, self).__setattr__(key, value)

    def _set_orig_attr(self, key, value):
        """Keep track which filed values are changed"""
        if key != '_id' and key not in self._meta.field_list:
            self._extra_fields.add(key)
        super(Model, self).__setattr__(key, value)

    @property
    def document_path(self):
        doc_path = self.collection_name + '/' + self._id
        if self.parent:
            doc_path = self.parent + '/' + doc_path

        return doc_path

    def document_reference(self):
        return db.conn.document(self.document_path)

    def _is_field_unchanged(self, field_name: str) -> bool:
        """Check if field has changed if possible.

        Return True if field has not changed, False if it has changed, or it is not possible to determine.
        """
        if field_name in self._field_changed:
            return True

        field = self._meta.field_list[field_name]
        value = getattr(self, field_name)
        if value is not None:
            if isinstance(field, (
                fields.MapField,
                fields.ListField,
                fields.NestedModelField,
            )):
                # Is unchanged check is not implemented for these field types yet
                return True

        if isinstance(field, fields.DateTime) and field.raw_attributes.get('auto_update', False):
            # Auto update fields are always considered changed
            return True

        return False

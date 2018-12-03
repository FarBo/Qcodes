import functools
import json
from typing import Any, Dict, List, Optional, Union, Sized, Callable
from threading import Thread
import time
import importlib
import logging
import uuid
from queue import Queue, Empty

from qcodes.dataset.param_spec import ParamSpec
from qcodes.instrument.parameter import _BaseParameter
from qcodes.dataset.sqlite_base import (atomic, atomic_transaction,
                                        transaction, add_parameter,
                                        connect, create_run, completed,
                                        is_column_in_table,
                                        get_experiments,
                                        get_last_experiment, select_one_where,
                                        length, modify_values,
                                        add_meta_data, mark_run_complete,
                                        modify_many_values, insert_values,
                                        insert_many_values,
                                        VALUE, VALUES, get_data,
                                        get_values,
                                        get_setpoints,
                                        get_metadata,
                                        get_metadata_from_run_id,
                                        one,
                                        get_experiment_name_from_experiment_id,
                                        get_sample_name_from_experiment_id,
                                        get_guid_from_run_id,
                                        get_runid_from_guid,
                                        get_run_timestamp_from_run_id,
                                        get_completed_timestamp_from_run_id,
                                        update_run_description,
                                        run_exists, remove_trigger,
                                        make_connection_plus_from,
                                        ConnectionPlus)
from qcodes.dataset.sqlite_storage_interface import SqliteStorageInterface
from qcodes.dataset.data_storage_interface import DataStorageInterface

from qcodes.dataset.sqlite_storage_interface import SqliteStorageInterface
from qcodes.dataset.data_storage_interface import DataStorageInterface

from qcodes.dataset.descriptions import RunDescriber
from qcodes.dataset.dependencies import InterDependencies
from qcodes.dataset.database import get_DB_location
from qcodes.dataset.guids import generate_guid
from qcodes.utils.deprecate import deprecate
import qcodes.config

log = logging.getLogger(__name__)

# TODO: as of now every time a result is inserted with add_result the db is
# saved same for add_results. IS THIS THE BEHAVIOUR WE WANT?

# TODO: storing parameters in separate table as an extension (dropping
# the column parametenrs would be much nicer

# TODO: metadata split between well known columns and maybe something else is
# not such a good idea. The problem is if we allow for specific columns then
# how do the user/us know which are metatadata?  I THINK the only sane solution
# is to store JSON in a column called metadata

# TODO: fixix  a subset of metadata that we define well known (and create them)
# i.e. no dynamic creation of metadata columns, but add stuff to
# a json inside a 'metadata' column

log = logging.getLogger(__name__)
SPECS = List[ParamSpec]


class CompletedError(RuntimeError):
    pass


class _Subscriber(Thread):
    """
    Class to add a subscriber to a DataSet. The subscriber gets called every
    time an insert is made to the results_table.

    The _Subscriber is not meant to be instantiated directly, but rather used
    via the 'subscribe' method of the DataSet.

    NOTE: A subscriber should be added *after* all parameters have been added.

    NOTE: Special care shall be taken when using the *state* object: it is the
    user's responsibility to operate with it in a thread-safe way.
    """
    def __init__(self,
                 dataSet: 'DataSet',
                 id_: str,
                 callback: Callable[..., None],
                 state: Optional[Any] = None,
                 loop_sleep_time: int = 0,  # in milliseconds
                 min_queue_length: int = 1,
                 callback_kwargs: Optional[Dict[str, Any]]=None
                 ) -> None:
        super().__init__()

        self._id = id_

        self.dataSet = dataSet
        self.table_name = dataSet.table_name
        self._data_set_len = len(dataSet)

        self.state = state

        self.data_queue: Queue = Queue()
        self._queue_length: int = 0
        self._stop_signal: bool = False
        self._loop_sleep_time = loop_sleep_time / 1000  # convert milliseconds to seconds
        self.min_queue_length = min_queue_length

        if callback_kwargs is None or len(callback_kwargs) == 0:
            self.callback = callback
        else:
            self.callback = functools.partial(callback, **callback_kwargs)

        self.callback_id = f"callback{self._id}"
        self.trigger_id = f"sub{self._id}"

        conn = dataSet.conn

        conn.create_function(self.callback_id, -1, self._cache_data_to_queue)

        parameters = dataSet.get_parameters()
        sql_param_list = ",".join([f"NEW.{p.name}" for p in parameters])
        sql_create_trigger_for_callback = f"""
        CREATE TRIGGER {self.trigger_id}
            AFTER INSERT ON '{self.table_name}'
        BEGIN
            SELECT {self.callback_id}({sql_param_list});
        END;"""
        atomic_transaction(conn, sql_create_trigger_for_callback)

        self.log = logging.getLogger(f"_Subscriber {self._id}")

    def _cache_data_to_queue(self, *args) -> None:
        self.log.debug(f"Args:{args} put into queue for {self.callback_id}")
        self.data_queue.put(args)
        self._data_set_len += 1
        self._queue_length += 1

    def run(self) -> None:
        self.log.debug("Starting subscriber")
        self._loop()

    @staticmethod
    def _exhaust_queue(queue: Queue) -> List:
        result_list = []
        while True:
            try:
                result_list.append(queue.get(block=False))
            except Empty:
                break
        return result_list

    def _call_callback_on_queue_data(self) -> None:
        result_list = self._exhaust_queue(self.data_queue)
        self.callback(result_list, self._data_set_len, self.state)
        self.log.debug(f"{self.callback} called with "
                       f"result_list: {result_list}.")

    def _loop(self) -> None:
        while True:
            if self._stop_signal:
                self._clean_up()
                break

            if self._queue_length >= self.min_queue_length:
                self._call_callback_on_queue_data()
                self._queue_length = 0

            time.sleep(self._loop_sleep_time)

            if self.dataSet.completed:
                self._call_callback_on_queue_data()
                break

    def done_callback(self) -> None:
        self.log.debug("Done callback")
        self._call_callback_on_queue_data()

    def schedule_stop(self) -> None:
        if not self._stop_signal:
            self.log.debug("Scheduling stop")
            self._stop_signal = True

    def _clean_up(self) -> None:
        self.log.debug("Stopped subscriber")


class DataSet(Sized):

    # the "persistent traits" are the attributes/properties of the DataSet
    # that are NOT tied to the representation of the DataSet in any particular
    # database
    persistent_traits = ('name', 'guid', 'number_of_results',
                         'parameters', 'paramspecs', 'exp_name', 'sample_name',
                         'completed', 'snapshot', 'run_timestamp_raw',
                         'description', 'completed_timestamp_raw', 'metadata')

    def __init__(self,
                 guid: Optional[str]=None,
                 storageinterface: type=SqliteStorageInterface,
                 **si_kwargs) -> None:
        """
        Create a new DataSet object. The object can either hold a new run or
        an already existing run.

        Args:
            guid: GUID of dataset. If not provided, a new dataset is created.
              If provided, the corresponding dataset is loaded (if it exists).
            storageinterface: The class of the storage interface to use for
              storing/loading the dataset
            si_kwargs: the kwargs to pass to the constructor of the
              storage interface
        """

        if not issubclass(storageinterface, DataStorageInterface):
            raise ValueError("The provided storage interface is not valid. "
                             "Must be a subclass of "
                             "qcodes.dataset.data_storage_interface."
                             "DataStorageInterface")

        if guid is not None:
            self._guid = guid
            self.dsi = storageinterface(self._guid, **si_kwargs)
            if not self.dsi.run_exists():
                raise ValueError(f'No run with GUID {guid} found.')

        else:
            self._guid = generate_guid()
            self.dsi = storageinterface(self._guid, **si_kwargs)
            self.dsi.create_run()

        # Assign all attributes
        run_meta_data = self.dsi.retrieve_meta_data()

        self._completed: bool = run_meta_data.run_completed is None
        self._started: bool = run_meta_data.run_started is None
        self._description = run_meta_data.run_description
        self._snapshot = run_meta_data.snapshot
        self._metadata = run_meta_data.tags

    @property
    def name(self):
        md = self.dsi.retrieve_meta_data()
        return md.name

    @property
    def guid(self):
        return self._guid

    @property
    def snapshot(self) -> Optional[dict]:
        """Snapshot of the run as dictionary (or None)"""
        md = self.dsi.retrieve_meta_data()
        return md.snapshot

    @property
    def snapshot_raw(self) -> Optional[str]:
        """Snapshot of the run as a JSON-formatted string (or None)"""
        return json.dumps(self.snapshot) if self.snapshot else None

    @property
    def number_of_results(self) -> int:
        self.dsi.retrieve_number_of_results()

    @property
    def counter(self):
        return select_one_where(self.conn, "runs",
                                "result_counter", "run_id", self.run_id)

    @property
    def parameters(self) -> str:
        idps = self._description.interdeps
        return ','.join([p.name for p in idps.paramspecs])

    @property
    def paramspecs(self) -> Dict[str, ParamSpec]:
        params = self.get_parameters()
        param_names = [p.name for p in params]
        return dict(zip(param_names, params))

    @property
    def exp_id(self) -> int:
        return getattr(self.dsi, 'exp_id', None)

    @property
    def exp_name(self) -> str:
        md = self.dsi.retrieve_meta_data()
        return md.exp_name

    @property
    def sample_name(self) -> str:
        md = self.dsi.retrieve_meta_data()
        return md.sample_name

    @property
    def run_timestamp_raw(self) -> float:
        """
        Returns run timestamp as number of seconds since the Epoch

        The run timestamp is the moment when the measurement for this run
        started.
        """
        md = self.dsi.retrieve_meta_data()
        return md.run_started

    @property
    def description(self) -> RunDescriber:
        return self._description

    @property
    def metadata(self) -> Dict:
        return self._metadata

    def the_same_dataset_as(self, other: 'DataSet') -> bool:
        """
        Check if two datasets correspond to the same run by comparing
        all their persistent traits. Note that this method
        does not compare the data itself.

        This function raises if the GUIDs match but anything else doesn't

        Args:
            other: the dataset to compare self to
        """

        if not isinstance(other, DataSet):
            return False

        guids_match = self.guid == other.guid

        for attr in DataSet.persistent_traits:
            if getattr(self, attr) != getattr(other, attr):
                if guids_match:
                    raise RuntimeError('Critical inconsistency detected! '
                                       'The two datasets have the same GUID,'
                                       f' but their "{attr}" differ.')
                else:
                    return False

        return True

    def run_timestamp(self, fmt: str="%Y-%m-%d %H:%M:%S") -> str:
        """
        Returns run timestamp in a human-readable format

        The run timestamp is the moment when the measurement for this run
        started.

        Consult with `time.strftime` for information about the format.
        """
        return time.strftime(fmt, time.localtime(self.run_timestamp_raw))

    @property
    def completed_timestamp_raw(self) -> Union[float, None]:
        """
        Returns timestamp when measurement run was completed
        as number of seconds since the Epoch

        If the run (or the dataset) is not completed, then returns None.
        """
        md = self.dsi.retrieve_meta_data()
        return md.run_completed

    def completed_timestamp(self,
                            fmt: str="%Y-%m-%d %H:%M:%S") -> Union[str, None]:
        """
        Returns timestamp when measurement run was completed
        in a human-readable format

        If the run (or the dataset) is not completed, then returns None.

        Consult with `time.strftime` for information about the format.
        """
        completed_timestamp_raw = self.completed_timestamp_raw

        if completed_timestamp_raw:
            completed_timestamp = time.strftime(
                fmt, time.localtime(completed_timestamp_raw))
        else:
            completed_timestamp = None

        return completed_timestamp

    def _get_run_description_from_db(self) -> RunDescriber:
        """
        Look up the run_description from the database
        """
        desc_str = select_one_where(self.conn, "runs", "run_description",
                                    "run_id", self.run_id)
        return RunDescriber.from_json(desc_str)

    def _perform_start_actions(self) -> None:
        """
        Perform the actions that must take place once the run has been started
        """
        # So far it is only one action: write down the run_description
        update_run_description(self.conn, self.run_id,
                               self.description.to_json())

    def toggle_debug(self):
        """
        Toggle debug mode, if debug mode is on all the queries made are
        echoed back.
        """
        self._debug = not self._debug
        self.conn.close()
        self.conn = connect(self.path_to_db, self._debug)

    def add_parameter(self, spec: ParamSpec):
        """
        Add a parameter to the DataSet. To ensure sanity, parameters must be
        added to the DataSet in a sequence matching their internal
        dependencies, i.e. first independent parameters, next other
        independent parameters inferred from the first ones, and finally
        the dependent parameters
        """
        if self.parameters:
            old_params = self.parameters.split(',')
        else:
            old_params = []

        if spec.name in old_params:
            raise ValueError(f'Duplicate parameter name: {spec.name}')

        inf_from = spec.inferred_from.split(', ')
        if inf_from == ['']:
            inf_from = []
        for ifrm in inf_from:
            if ifrm not in old_params:
                raise ValueError('Can not infer parameter '
                                 f'{spec.name} from {ifrm}, '
                                 'no such parameter in this DataSet')

        dep_on = spec.depends_on.split(', ')
        if dep_on == ['']:
            dep_on = []
        for dp in dep_on:
            if dp not in old_params:
                raise ValueError('Can not have parameter '
                                 f'{spec.name} depend on {dp}, '
                                 'no such parameter in this DataSet')

        add_parameter(self.conn, self.table_name, spec)

        desc = self.description
        desc.interdeps = InterDependencies(*desc.interdeps.paramspecs, spec)
        self._description = desc

    def get_parameters(self) -> SPECS:
        return self.description.interdeps.paramspecs

    @deprecate(reason=None, alternative="DataSet.add_parameter")
    def add_parameters(self, specs: SPECS) -> None:
        add_parameter(self.conn, self.table_name, *specs)

    def add_metadata(self, tag: str, metadata: Any):
        """
        Adds metadata to the DataSet. The metadata is stored under the
        provided tag. Note that None is not allowed as a metadata value.

        Args:
            tag: represents the key in the metadata dictionary
            metadata: actual metadata
        """

        self._metadata[tag] = metadata
        # `add_meta_data` is not atomic by itself, hence using `atomic`
        with atomic(self.conn) as conn:
            add_meta_data(conn, self.run_id, {tag: metadata})

    def add_snapshot(self, snapshot: str, overwrite: bool=False) -> None:
        """
        Adds a snapshot to this run

        Args:
            snapshot: the raw JSON dump of the snapshot
            overwrite: force overwrite an existing snapshot
        """
        if self.snapshot is None or overwrite:
            add_meta_data(self.conn, self.run_id, {'snapshot': snapshot})
        elif self.snapshot is not None and not overwrite:
            log.warning('This dataset already has a snapshot. Use overwrite'
                        '=True to overwrite that')

    @property
    def started(self) -> bool:
        return self._started

    @property
    def completed(self) -> bool:
        return self._completed

    @completed.setter
    def completed(self, value):
        self._completed = value
        if value:
            mark_run_complete(self.conn, self.run_id)

    def mark_complete(self) -> None:
        """
        Mark dataset as complete and thus read only and notify the subscribers
        """
        self.completed = True
        for sub in self.subscribers.values():
            sub.done_callback()

    def add_result(self, results: Dict[str, VALUE]) -> None:
        """
        Add a logically single result to existing parameters

        Args:
            results: dictionary with name of a parameter as the key and the
                value to associate as the value.

        Returns:
            index in the DataSet that the result was stored at

        If a parameter exist in the dataset and it's not in the results
        dictionary, "Null" values are inserted.

        It is an error to provide a value for a key or keyword that is not
        the name of a parameter in this DataSet.

        It is an error to add results to a completed DataSet.
        """

        if not self.started:
            self._perform_start_actions()
            self._started = True

        # TODO: Make this check less fugly
        for param in results.keys():
            if self.paramspecs[param].depends_on != '':
                deps = self.paramspecs[param].depends_on.split(', ')
                for dep in deps:
                    if dep not in results.keys():
                        raise ValueError(f'Can not add result for {param}, '
                                         f'since this depends on {dep}, '
                                         'which is not being added.')

        if self.completed:
            raise CompletedError

        self.data_storage_interface.store_results(
            {k: [v] for k, v in results.items()})

    def add_results(self, results: List[Dict[str, VALUE]]) -> int:
        """
        Adds a sequence of results to the DataSet.

        Args:
            results: list of name-value dictionaries where each dictionary
                provides the values for the parameters in that result. If some
                parameters are missing the corresponding values are assumed
                to be None

        Returns:
            the index in the DataSet that the **first** result was stored at

        It is an error to provide a value for a key or keyword that is not
        the name of a parameter in this DataSet.

        It is an error to add results to a completed DataSet.
        """

        if not self.started:
            self._perform_start_actions()
            self._started = True

        expected_keys = tuple(frozenset.union(*[frozenset(d) for d in results]))
        values = [[d.get(k, None) for k in expected_keys] for d in results]
        values_transposed = list(map(list, zip(*values)))

        len_before_add = length(self.conn, self.table_name)

        self.data_storage_interface.store_results(
            {k: v for k, v in zip(expected_keys, values_transposed)})

        return len_before_add

    @deprecate(reason='it is an experimental functionality, and is likely '
                      'to be removed soon.')
    def modify_result(self, index: int, results: Dict[str, VALUES]) -> None:
        """
        Modify a logically single result of existing parameters

        Args:
            index: zero-based index of the result to be modified.
            results: dictionary of updates with name of a parameter as the
               key and the value to associate as the value.

        It is an error to modify a result at an index less than zero or
        beyond the end of the DataSet.

        It is an error to provide a value for a key or keyword that is not
        the name of a parameter in this DataSet.

        It is an error to modify a result in a completed DataSet.
        """
        if self.completed:
            raise CompletedError

        for param in results.keys():
            if param not in self.paramspecs.keys():
                raise ValueError(f'No such parameter: {param}.')

        with atomic(self.conn) as conn:
            modify_values(conn, self.table_name, index,
                          list(results.keys()),
                          list(results.values())
                          )

    @deprecate(reason='it is an experimental functionality, and is likely '
                      'to be removed soon.',
               alternative='modify_result')
    def modify_results(self, start_index: int,
                       updates: List[Dict[str, VALUES]]):
        """
        Modify a sequence of results in the DataSet.

        Args:
            index: zero-based index of the result to be modified.
            results: sequence of dictionares of updates with name of a
                parameter as the key and the value to associate as the value.


        It is an error to modify a result at an index less than zero or
        beyond the end of the DataSet.

        It is an error to provide a value for a key or keyword that is not
        the name of a parameter in this DataSet.

        It is an error to modify a result in a completed DataSet.
        """
        if self.completed:
            raise CompletedError

        keys = [list(val.keys()) for val in updates]
        flattened_keys = [item for sublist in keys for item in sublist]

        mod_params = set(flattened_keys)
        old_params = set(self.paramspecs.keys())
        if not mod_params.issubset(old_params):
            raise ValueError('Can not modify values for parameter(s) '
                             f'{mod_params.difference(old_params)}, '
                             'no such parameter(s) in the dataset.')

        values = [list(val.values()) for val in updates]
        flattened_values = [item for sublist in values for item in sublist]

        with atomic(self.conn) as conn:
            modify_many_values(conn,
                               self.table_name,
                               start_index,
                               flattened_keys,
                               flattened_values)

    @deprecate(reason='it is an experimental functionality, and is likely '
                      'to be removed soon.',
               alternative='add_parameter, add_result, add_results')
    def add_parameter_values(self, spec: ParamSpec, values: VALUES):
        """
        Add a parameter to the DataSet and associates result values with the
        new parameter.

        If the DataSet is not empty, then the count of provided values must
        equal the current count of results in the DataSet, or an error will
        be raised.

        It is an error to add parameters to a completed DataSet.
        """
        # first check that the len of values (if dataset is not empty)
        # is the right size i.e. the same as the dataset
        if len(self) > 0:
            if len(values) != len(self):
                raise ValueError("Need to have {} values but got {}.".format(
                    len(self),
                    len(values)
                ))

        with atomic(self.conn) as conn:
            add_parameter(conn, self.table_name, spec)
            # now add values!
            results = [{spec.name: value} for value in values]
            log.info(f"add_parameter_values:add_result: {results}")
            self.add_results(results)

    def get_data(self,
                 *params: Union[str, ParamSpec, _BaseParameter],
                 start: Optional[int] = None,
                 end: Optional[int] = None) -> List[List[Any]]:
        """
        Returns the values stored in the DataSet for the specified parameters.
        The values are returned as a list of lists, SQL rows by SQL columns,
        e.g. datapoints by parameters. The data type of each element is based
        on the datatype provided when the DataSet was created. The parameter
        list may contain a mix of string parameter names, QCoDeS Parameter
        objects, and ParamSpec objects (as long as they have a `name` field).

        If provided, the start and end arguments select a range of results
        by result count (index). If the range is empty - that is, if the end is
        less than or equal to the start, or if start is after the current end
        of the DataSet – then a list of empty arrays is returned.

        For a more type independent and easier to work with view of the data
        you may want to consider using
        :py:meth:`qcodes.dataset.data_export.get_data_by_id`

        Args:
            *params: string parameter names, QCoDeS Parameter objects, and
                ParamSpec objects
            start: start value of selection range (by result count); ignored
                if None
            end: end value of selection range (by results count); ignored if
                None

        Returns:
            list of lists SQL rows of data by SQL columns. Each SQL row is a
            datapoint and each SQL column is a parameter. Each element will
            be of the datatypes stored in the database (numeric, array or
            string)
        """
        valid_param_names = []
        for maybeParam in params:
            if isinstance(maybeParam, str):
                valid_param_names.append(maybeParam)
                continue
            else:
                try:
                    maybeParam = maybeParam.name
                except Exception as e:
                    raise ValueError(
                        "This parameter does not have  a name") from e
            valid_param_names.append(maybeParam)
        data = get_data(self.conn, self.table_name, valid_param_names,
                        start, end)
        return data

    def get_values(self, param_name: str) -> List[List[Any]]:
        """
        Get the values (i.e. not NULLs) of the specified parameter
        """
        if param_name not in self.parameters:
            raise ValueError('Unknown parameter, not in this DataSet')

        values = get_values(self.conn, self.table_name, param_name)

        return values

    def get_setpoints(self, param_name: str) -> Dict[str, List[List[Any]]]:
        """
        Get the setpoints for the specified parameter

        Args:
            param_name: The name of the parameter for which to get the
                setpoints
        """

        if param_name not in self.parameters:
            raise ValueError('Unknown parameter, not in this DataSet')

        if self.paramspecs[param_name].depends_on == '':
            raise ValueError(f'Parameter {param_name} has no setpoints.')

        setpoints = get_setpoints(self.conn, self.table_name, param_name)

        return setpoints

    def subscribe(self,
                  callback: Callable[[Any, int, Optional[Any]], None],
                  min_wait: int = 0,
                  min_count: int = 1,
                  state: Optional[Any] = None,
                  callback_kwargs: Optional[Dict[str, Any]] = None
                  ) -> str:
        subscriber_id = uuid.uuid4().hex
        subscriber = _Subscriber(self, subscriber_id, callback, state,
                                 min_wait, min_count, callback_kwargs)
        self.subscribers[subscriber_id] = subscriber
        subscriber.start()
        return subscriber_id

    def subscribe_from_config(self, name: str) -> str:
        """
        Subscribe a subscriber defined in the `qcodesrc.json` config file to
        the data of this `DataSet`. The definition can be found at
        `subscription.subscribers`.

        Args:
            name: identifier of the subscriber. Equal to the key of the entry
                in 'qcodesrc.json::subscription.subscribers'.
        """
        subscribers = qcodes.config.subscription.subscribers
        try:
            subscriber_info = getattr(subscribers, name)
        # the dot dict behind the config does not convert the error and
        # actually raises a `KeyError`
        except (AttributeError, KeyError):
            keys = ','.join(subscribers.keys())
            raise RuntimeError(
                f'subscribe_from_config: failed to subscribe "{name}" to DataSet '
                f'from list of subscribers in `qcodesrc.json` (subscriptions.'
                f'subscribers). Chose one of: {keys}')
        # get callback from string
        parts = subscriber_info.factory.split('.')
        import_path, type_name = '.'.join(parts[:-1]), parts[-1]
        module = importlib.import_module(import_path)
        factory = getattr(module, type_name)

        kwargs = {k: v for k, v in subscriber_info.subscription_kwargs.items()}
        kwargs['callback'] = factory(self, **subscriber_info.factory_kwargs)
        kwargs['state'] = {}
        return self.subscribe(**kwargs)

    def unsubscribe(self, uuid: str) -> None:
        """
        Remove subscriber with the provided uuid
        """
        with atomic(self.conn) as conn:
            sub = self.subscribers[uuid]
            remove_trigger(conn, sub.trigger_id)
            sub.schedule_stop()
            sub.join()
            del self.subscribers[uuid]

    def unsubscribe_all(self):
        """
        Remove all subscribers
        """
        sql = "select * from sqlite_master where type = 'trigger';"
        triggers = atomic_transaction(self.conn, sql).fetchall()
        with atomic(self.conn) as conn:
            for trigger in triggers:
                remove_trigger(conn, trigger['name'])
            for sub in self.subscribers.values():
                sub.schedule_stop()
                sub.join()
            self.subscribers.clear()

    def get_metadata(self, tag):
        return get_metadata(self.conn, tag, self.table_name)

    def __len__(self) -> int:
        return length(self.conn, self.table_name)

    def __repr__(self) -> str:
        out = []
        heading = f"{self.name} #{self.run_id}@{self.path_to_db}"
        out.append(heading)
        out.append("-" * len(heading))
        ps = self.get_parameters()
        if len(ps) > 0:
            for p in ps:
                out.append(f"{p.name} - {p.type}")

        return "\n".join(out)


# public api
def load_by_id(run_id: int, conn: Optional[ConnectionPlus]=None) -> DataSet:
    """
    Load dataset by run id

    If no connection is provided, lookup is performed in the database file that
    is specified in the config.

    Args:
        run_id: run id of the dataset
        conn: connection to the database to load from

    Returns:
        dataset with the given run id
    """
    if run_id is None:
        raise ValueError('run_id has to be a positive integer, not None.')

    conn = conn or connect(get_DB_location())

    d = DataSet(conn=conn, run_id=run_id)
    return d


def load_by_guid(guid: str, conn: Optional[ConnectionPlus]=None) -> DataSet:
    """
    Load a dataset by its GUID

    If no connection is provided, lookup is performed in the database file that
    is specified in the config.

    Args:
        guid: guid of the dataset
        conn: connection to the database to load from

    Returns:
        dataset with the given guid

    Raises:
        NameError if no run with the given GUID exists in the database
        RuntimeError if several runs with the given GUID are found
    """
    conn = conn or connect(get_DB_location())

    # this function raises a RuntimeError if more than one run matches the GUID
    run_id = get_runid_from_guid(conn, guid)

    if run_id == -1:
        raise NameError(f'No run with GUID: {guid} found in database.')

    return DataSet(run_id=run_id, conn=conn)


def load_by_counter(counter: int, exp_id: int,
                    conn: Optional[ConnectionPlus]=None) -> DataSet:
    """
    Load a dataset given its counter in a given experiment

    Lookup is performed in the database file that is specified in the config.

    Args:
        counter: counter of the dataset within the given experiment
        exp_id: id of the experiment where to look for the dataset
        conn: connection to the database to load from. If not provided, a
          connection to the DB file specified in the config is made

    Returns:
        dataset of the given counter in the given experiment
    """
    conn = conn or connect(get_DB_location())
    sql = """
    SELECT run_id
    FROM
      runs
    WHERE
      result_counter= ? AND
      exp_id = ?
    """
    c = transaction(conn, sql, counter, exp_id)
    run_id = one(c, 'run_id')

    d = DataSet(conn=conn, run_id=run_id)
    return d


def new_data_set(name, exp_id: Optional[int] = None,
                 specs: SPECS = None, values=None,
                 metadata=None, conn=None) -> DataSet:
    """
    Create a new dataset in the currently active/selected database.

    If exp_id is not specified, the last experiment will be loaded by default.

    Args:
        name: the name of the new dataset
        exp_id: the id of the experiments this dataset belongs to, defaults
            to the last experiment
        specs: list of parameters to create this dataset with
        values: the values to associate with the parameters
        metadata: the metadata to associate with the dataset

    Return:
        the newly created dataset
    """
    # note that passing `conn` is a secret feature that is unfortunately used
    # in `Runner` to pass a connection from an existing `Experiment`.
    d = DataSet(path_to_db=None, run_id=None, conn=conn,
                name=name, specs=specs, values=values,
                metadata=metadata, exp_id=exp_id)

    return d
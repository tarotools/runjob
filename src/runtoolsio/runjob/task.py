from enum import Enum

from tarotools.taro import util
from tarotools.taro.util import convert_if_number


class Fields(Enum):
    EVENT = 'event'
    TASK = 'task'
    TIMESTAMP = 'timestamp'
    COMPLETED = 'completed'
    INCREMENT = 'increment'
    TOTAL = 'total'
    UNIT = 'unit'
    RESULT = 'result'


DEFAULT_PATTERN = ''


def field_conversion(parsed):
    converted = {
        Fields.EVENT: parsed.get(Fields.EVENT.value),
        Fields.TASK: parsed.get(Fields.TASK.value),
        Fields.TIMESTAMP: util.parse_datetime(parsed.get(Fields.TIMESTAMP.value)),
        Fields.COMPLETED: convert_if_number(parsed.get(Fields.COMPLETED.value)),
        Fields.INCREMENT: convert_if_number(parsed.get(Fields.INCREMENT.value)),
        Fields.TOTAL: convert_if_number(parsed.get(Fields.TOTAL.value)),
        Fields.UNIT: parsed.get(Fields.UNIT.value),
        Fields.RESULT: parsed.get(Fields.RESULT.value),
    }

    return {key: value for key, value in converted.items() if value is not None}


class TaskOutputParser:

    def __init__(self, task_tracker, parsers, conversion=field_conversion):
        self.task = task_tracker
        self.parsers = parsers
        self.conversion = conversion

    def __call__(self, output, is_error=False):
        self.new_output(output, is_error)

    def new_output(self, output, is_error=False):
        parsed = {}
        for parser in self.parsers:
            if p := parser(output):
                parsed.update(p)

        if not parsed:
            return

        fields = self.conversion(parsed)
        if not fields:
            return

        task = self._update_task(fields)
        if not self._update_operation(task, fields):
            task.add_event(fields.get(Fields.EVENT), fields.get(Fields.TIMESTAMP))

    def _update_task(self, fields):
        task = fields.get(Fields.TASK)
        if task:
            rel_task = self.task.subtask(task)
            self.task.active = False
        else:
            rel_task = self.task

        if not rel_task.first_updated_at:
            rel_task.first_updated_at = fields.get(Fields.TIMESTAMP)
        rel_task.last_update_at = fields.get(Fields.TIMESTAMP)
        rel_task.active = True
        rel_task.deactivate_subtasks()
        rel_task.deactivate_finished_operations()
        result = fields.get(Fields.RESULT)
        if result:
            rel_task.result = result

        return rel_task

    def _update_operation(self, task, fields):
        op_name = fields.get(Fields.EVENT)
        ts = fields.get(Fields.TIMESTAMP)
        completed = fields.get(Fields.COMPLETED)
        increment = fields.get(Fields.INCREMENT)
        total = fields.get(Fields.TOTAL)
        unit = fields.get(Fields.UNIT)

        if not completed and not increment and not total and not unit:
            return False

        if not task.has_operation(op_name):
            task.reset_current_event()

        task.operation(op_name).update(completed or increment, total, unit, ts, increment=increment is not None)
        return True

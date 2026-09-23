"""Give routing fixtures explicit isolated resolve execution authority."""

from dataclasses import fields

from tests.fakes.accounted_workflow import AccountedFixtureWorkflow, attach_workflow_authority


def with_resolve_authority(dispatcher, *, resolve_ceiling=3):
    workflow = dispatcher.fix_workflow
    values = {field.name: getattr(workflow, field.name) for field in fields(workflow)}
    values["workflow_control"] = attach_workflow_authority(
        workflow.gateway, workflow.store, resolve_ceiling=resolve_ceiling,
    )
    dispatcher.fix_workflow = AccountedFixtureWorkflow(**values)
    return dispatcher

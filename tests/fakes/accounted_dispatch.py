"""Give routing fixtures explicit isolated resolve execution authority."""

from dataclasses import fields, replace

from tests.fakes.accounted_workflow import AccountedFixtureWorkflow, attach_workflow_authority


def with_resolve_authority(dispatcher, *, resolve_ceiling=3):
    workflow = dispatcher.fix_workflow
    values = {field.name: getattr(workflow, field.name) for field in fields(workflow)}
    if workflow.cfg.operator_guidance is None:
        values["cfg"] = replace(workflow.cfg, operator_guidance=lambda pr: getattr(
            workflow.store, "operator_guidance", {}).get(pr, ""))
    values["workflow_control"] = attach_workflow_authority(
        workflow.gateway,
        workflow.store,
        resolve_ceiling=resolve_ceiling,
        max_contract_rounds=workflow.cfg.max_contract_rounds,
        pr_repair=True,
    )
    dispatcher.fix_workflow = AccountedFixtureWorkflow(**values)
    object.__setattr__(dispatcher.fix_workflow, "_fixture_infra_ceiling", dispatcher.cfg.max_infra_deaths)
    return dispatcher

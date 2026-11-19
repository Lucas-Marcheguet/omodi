/** @odoo-module **/
import { registry } from "@web/core/registry";
registry.category("actions").add("demo_action", Foo);
registry.category("services").add("demo_service", svc, { sequence: 10 });
registry.category("web").category("sub").add("k", 1);

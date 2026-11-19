/** @odoo-module **/
import { registry } from "@web/core/registry";
registry.category("actions").add("demo_action", Bar);
registry.category("web").category("sub").add("k", 1);
registry.category("actions").add("demo_new", Foo);

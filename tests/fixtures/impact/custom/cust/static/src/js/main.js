/** @odoo-module **/
import { Base, Gone, Moved, Kept } from "@up/js/lib";
import { registry } from "@web/core/registry";
import { patch } from "@web/core/utils/patch";

export class MyBase extends Base {
    removedMethod() {}
    changedMethod(a) {}
    setup() {}
}
patch(Base.prototype, { removedMethod() {}, changedMethod(a) {}, setup() {} });
registry.category("main").add("entry_gone", Gone);
registry.category("main").add("entry_new", Kept);

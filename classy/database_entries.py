import idaapi
import idc
import database
import itanium_mangler


class Class(object):
    def __init__(self, name, base):

        self.name = name

        self.base = base
        self.derived = []

        if self.base:
            self.base.derived.append(self)

        self.methods = []

        self.vtable_start = None
        self.vtable_end = None
        self.vmethods = []
        self.reset_vtable()

        db = database.get()
        db.classes_by_name[name] = self


    def unlink(self):
        if len(self.derived) > 0:
            raise ValueError('Cannot unlink classes with derived classes')

        for m in self.methods:
            m.unlink()

        for vm in self.vmethods:
            if vm.owner == self:
                vm.unlink()

        if self.base is not None:
            self.base.derived.remove(self)

        db = database.get()
        del db.classes_by_name[self.name]


    def rename(self, new_name):
        old_name = self.name

        db = database.get()
        del db.classes_by_name[old_name]
        db.classes_by_name[new_name] = self

        self.name = new_name

        # Rename ctors and dtors
        for vm in self.vmethods:
            if vm.name == old_name:
                vm.name = new_name
            if vm.name == '~' + old_name:
                vm.name = '~' + new_name
        for m in self.methods:
            if m.name == old_name:
                m.name = new_name
            if m.name == '~' + old_name:
                m.name = '~' + new_name

        self.refresh()


    def refresh(self):
        for m in self.methods:
            m.refresh()
        for m in self.vmethods:
            m.refresh()


    def set_vtable_range(self, start, end):
        if self.is_vtable_locked():
            raise ValueError('VTable cannot be modified because the class has derived classes')
        if start % 4 or end % 4:
            raise ValueError('VTable start and end must be 4 byte aligned')
        if start >= end:
            raise ValueError('Vtable end must be after the start')
        if self.base:
            new_len = (end - start) // 4
            if new_len < len(self.base.vmethods):
                raise ValueError('VTable is smaller than base VTable')
        # Todo: More sanity checks: Don't overwrite any other vtable

        self.reset_vtable()
        self.vtable_start = start
        self.vtable_end = end
        self.init_vtable()


    def is_vtable_locked(self):
        return len(self.derived) > 0


    def can_be_derived(self):
        if self.base is None:
            return True
        return len(self.vmethods) >= len(self.base.vmethods)    # vtable inited?


    def vtable_start_idx(self):
        if self.base is None:
            return 0
        return len(self.base.vmethods)


    def reset_vtable(self):
        if self.is_vtable_locked():
            return
        self.vtable_start = None
        self.vtable_end = None
        idx = self.vtable_start_idx()
        for vm in self.vmethods[idx:]:
            vm.unlink()
        self.vmethods = []


    def init_vtable(self):
        idx = 0
        my_start_idx = self.vtable_start_idx()

        for ea in range(self.vtable_start, self.vtable_end, 4):
            idc.MakeDword(ea)
            idc.OpOff(ea, 0, 0)
            dst = idc.Dword(ea)

            if idx < my_start_idx:
                base_method = self.base.vmethods[idx]
                if dst == base_method.ea:                           # Method from base class
                    self.vmethods.append(self.base.vmethods[idx])
                else:                                               # Override
                    om = OverrideMethod(dst, self, base_method)
                    om.refresh()
                    self.vmethods.append(om)
            else:                                                   # New virtual
                vm = VirtualMethod(dst, self, 'vf%d' % idx)
                vm.refresh()
                self.vmethods.append(vm)

            idx += 1


    def iter_vtable(self):
        ea = self.vtable_start
        end = self.vtable_end

        while ea <= end:
            yield (ea, idc.Dword(ea))
            ea += 4


    @staticmethod
    def s_name_is_valid(name):

        segs = name.split('::')

        if len(segs) == 0:
            return False

        for seg in segs:
            if len(seg) < 1:
                return False

            if seg[0].isdigit():
                return False

            for c in seg:
                if not c.isalnum() and c != '_':
                    return False

        return True


    @staticmethod
    def s_create():
        db = database.get()

        name = idaapi.askqstr('', 'Enter a class name')
        if name in database.get().classes_by_name:
            idaapi.warning('That name is already used.')
            return

        if name is None:
            return

        if not Class.s_name_is_valid(name):
            idaapi.warning('The class name "%s" is invalid.' % name)
            return

        base_class = None
        base_name = idaapi.askqstr('', 'Enter a base class name (leave empty for none)')
        if base_name is None:
            return
        if base_name:
            if base_name not in db.classes_by_name:
                idaapi.warning('The class "%s" is not in the database.' % base_name)
                return
            else:
                base_class = db.classes_by_name[base_name]
                if not base_class.can_be_derived():
                    idaapi.warning('The class %s cannot be derived because the VTable is not setup correctly')
                    return

        Class(name, base_class)

        '''
        safe_name = name.replace('::', '_')
    
        struct = idaapi.get_struc_id(safe_name)
        if struct != idaapi.BADADDR:
            if struct in database.get().classes_by_struct.keys():
                idaapi.warning('The struct "%s" is already associated with a struct!' % safe_name)
                return
    
            if not util.ask_yes_no('The struct "%s" already exists. Continue?' % safe_name, True):
                return
    
        else:
            struct = idaapi.add_struc(idaapi.BADADDR, safe_name, 0)
            if struct == idaapi.BADADDR:
                idaapi.warning('Creating struct "%s" for class "%s" failed!' % (safe_name, name))
                return
        '''


class Method(object):
    def __init__(self, ea, owner, name):
        self.ea = ea
        self.owner = owner
        self.name = name
        self.args = ''
        self.return_type = 'void'
        self.is_const = False
        self.ctor_type = 1
        self.dtor_type = 1

        if ea != idc.BADADDR:
            database.get().known_methods[ea] = self


    def type_name(self):
        return 'regular'


    def refresh(self):
        mangled = self.get_mangled()
        idc.MakeName(self.ea, mangled)
        self.refresh_comment()


    def unlink(self):
        if self.owner and self in self.owner.methods:
            self.owner.methods.remove(self)

        self.owner = None
        del database.get().known_methods[self.ea]
        idc.MakeName(self.ea, '')
        idc.SetFunctionCmt(self.ea, '', False)


    def set_signature(self, name, args, return_type='void', is_const=False, ctor_type=1, dtor_type=1):
        signature = Method.s_make_signature(self.owner, name, args, is_const, return_type)
        itanium_mangler.mangle_function(signature, ctor_type, dtor_type)    # throws excption when invalid
        self.name = name
        self.args = args
        self.return_type = return_type
        self.is_const = is_const
        self.ctor_type = ctor_type
        self.dtor_type = dtor_type
        self.refresh()


    @staticmethod
    def s_make_signature(owner, name, args='', is_const=False, return_type=''):
        signature = ('%s::' % owner.name) if owner is not None else ''
        signature += name
        signature += '('
        signature += args
        signature += ')'
        if is_const:
            signature += ' const'
        if return_type:
            signature = return_type + ' ' + signature
        return signature


    def get_signature(self, include_return_type=True):
        return Method.s_make_signature(self.owner, self.name, self.args, self.is_const, self.return_type if include_return_type else '')


    def copy_signature(self, other):
        if (other.owner is not None) and (other.name == '~' + other.owner.name):
            if self.owner is None:
                raise ValueError('Cannot copy dtor to non-owned function')
            self.name = '~' + self.owner.name
        else:
            self.name = other.name
        self.args = other.args
        self.return_type = other.return_type
        self.is_const = other.is_const
        self.ctor_type = other.ctor_type
        self.dtor_type = other.dtor_type


    def get_mangled(self):
        demangled = self.get_signature(False)
        return itanium_mangler.mangle_function(demangled, self.ctor_type, self.dtor_type)


    def get_comment(self):
        return ''


    def refresh_comment(self):
        comment = self.get_comment()
        if comment:
            idc.SetFunctionCmt(self.ea, comment, False)



class VirtualMethod(Method):
    def __init__(self, ea, owner, name):
        super(VirtualMethod, self).__init__(ea, owner, name)
        self.overrides = []


    def type_name(self):
        return 'virtual'


    def refresh(self):
        Method.refresh(self)


    def unlink(self):
        if len(self.overrides) > 0:
            raise ValueError('Cannot unlink method with overrides')
        for i, vm in enumerate(self.owner.vmethods):
            if vm == self:
                self.owner.vmethods[i] = None
        Method.unlink(self)


    def set_signature(self, name, args, return_type='void', is_const=False, ctor_type=1, dtor_type=1):
        Method.set_signature(self, name, args, return_type, is_const, ctor_type, dtor_type)
        for o in self.overrides:
            o.propagate_signature()


    def get_comment(self):
        lines = []

        if len(self.overrides) > 0:
            lines.append('Overridden by:')
            for o in self.overrides:
                lines.append('  - %s : 0x%X' % (o.owner.name, o.ea))
        else:
            lines.append('Overridden by: None')

        return "\n".join(lines)


    def add_override(self, override):
        if override in self.overrides:
            return
        self.overrides.append(override)
        self.refresh_comment()


    def remove_override(self, override):
        if override not in self.overrides:
            return
        self.overrides.remove(override)
        self.refresh_comment()



class OverrideMethod(VirtualMethod):
    def __init__(self, ea, owner, base):
        if not base.owner.can_be_derived:
            raise ValueError('Overriding function of class without inited VTable')
        super(OverrideMethod, self).__init__(ea, owner, base.name)
        self.base = base
        self.base.add_override(self)
        self.copy_signature(base)


    def type_name(self):
        return 'override'


    def unlink(self):
        self.base.remove_override(self)
        VirtualMethod.unlink(self)


    def set_signature(self, name, args, return_type='void', is_const=False, ctor_type=1, dtor_type=1):
        root_method = self.get_root_method()

        if name == '~' + self.owner.name:
            root_name = '~' + root_method.owner.name
        else:
            root_name = name

        root_method.set_signature(root_name, args, return_type, is_const, ctor_type, dtor_type)


    def propagate_signature(self):
        self.copy_signature(self.base)
        self.refresh()
        for o in self.overrides:
            self.propagate_signature()


    def get_root_method(self):
        method = self
        while type(method) != VirtualMethod:
            method = method.base
        return method


    def get_comment(self):
        return 'Overrides: %s : 0x%X\n\n%s' % (self.base.owner.name, self.base.ea, VirtualMethod.get_comment(self))



class NullMethod(Method):
    def __init__(self, owner):
        super(NullMethod, self).__init__(idc.BADADDR, owner, 'NullMethod')


    def type_name(self):
        return 'null'


    def refresh(self):
        pass


    def unlink(self):
        pass


def refresh_all():
    db = database.get()

    for c in db.classes_by_name.values():
        c.refresh()
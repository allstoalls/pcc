from inherited_private_fields_provider import Base, DescriptorBase
import gc


class Child(Base):
    __value = None

    def write_child(self, value):
        self.__value = value

    def read_child(self):
        return self.__value

    def callback(self):
        print('callback', self.read(), self.read_public(), self.read_child())


class DescriptorChild(DescriptorBase):
    pass


def main():
    child = Child()
    print('default', child.read(), child.read_public(), child.read_child())
    child.write_child('child')
    child.write('base')
    gc.collect()
    print('stored', child.read(), child.read_public(), child.read_child())
    child.write(None)
    descriptor = DescriptorChild()
    descriptor.write()
    print('descriptors', descriptor.read())
    gc.collect()


main()

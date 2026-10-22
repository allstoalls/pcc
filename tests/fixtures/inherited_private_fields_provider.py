class Base:
    public = None
    __value = None

    def write(self, value):
        self.public = value
        self.__value = None
        self.__value = value
        self.callback()

    def read(self):
        return self.__value

    def read_public(self):
        return self.public

    def callback(self):
        pass


class DataDescriptor:
    def __get__(self, instance, owner):
        return 'data-descriptor'

    def __set__(self, instance, value):
        instance.saved = value


class NonDataDescriptor:
    def __get__(self, instance, owner):
        return 'nondata-descriptor'


class DescriptorBase:
    data = DataDescriptor()
    nondata = NonDataDescriptor()

    def write(self):
        self.data = 'stored-through-descriptor'
        self.nondata = 'instance-override'

    def read(self):
        return self.data, self.nondata, self.saved

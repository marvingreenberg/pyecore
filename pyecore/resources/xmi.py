from lxml import etree
from .resource import Resource
import pyecore.ecore as Ecore

xsi = 'xsi'
xsi_url = 'http://www.w3.org/2001/XMLSchema-instance'
xmi = 'xmi'
xmi_url = 'http://www.omg.org/XMI'


class XMIResource(Resource):
    def __init__(self, uri=None, use_uuid=False):
        super().__init__(uri, use_uuid)

    def load(self):
        self._meta_cache = {}
        tree = etree.parse(self.uri.create_instream())
        xmlroot = tree.getroot()
        self.prefixes.update(xmlroot.nsmap)
        self.reverse_nsmap = {v: k for k, v in self.prefixes.items()}
        XMIResource.xsitype = '{{{0}}}type'.format(self.prefixes[xsi])
        XMIResource.xmiid = '{{{0}}}id'.format(self.prefixes[xmi])
        # Decode the XMI
        modelroot = self._init_modelroot(xmlroot)
        if not self.contents:
            self._clean_registers()
            return
        for child in xmlroot:
            self._decode_eobject(child, modelroot)
        self._decode_ereferences()
        self._clean_registers()
        self.uri.close_stream()

    def resolve(self, fragment):
        fragment = Resource.normalize(fragment)
        if fragment in self._resolve_mem:
            return self._resolve_mem[fragment]
        if self._use_uuid:
            try:
                frag = fragment[1:] if fragment.startswith('#') \
                                    else fragment
                frag = frag[2:] if frag.startswith('//') else frag
                return self.uuid_dict[frag]
            except KeyError:
                pass
        result = None
        for root in self._contents:
            result = Resource._navigate_from(fragment, root)
            if result:
                self._resolve_mem[fragment] = result
                return result

    def extract_namespace(tag):
        qname = etree.QName(tag)
        return qname.namespace, qname.localname

    def _find_in_metacache(self, obj, name):
        fname = obj.eClass.name + '#' + name
        try:
            return self._meta_cache[fname]
        except KeyError:
            feat = obj.eClass.findEStructuralFeature(name)
            self._meta_cache[fname] = feat
            return feat

    def _type_attribute(self, node):
        return node.get(XMIResource.xsitype)

    def _init_modelroot(self, xmlroot):
        nsURI, eclass_name = XMIResource.extract_namespace(xmlroot.tag)
        eobject = self.get_metamodel(nsURI).getEClassifier(eclass_name)
        if not eobject:
            raise TypeError({'{0} EClass does not exists'}.format(eclass_name))
        modelroot = eobject()
        modelroot._eresource = self
        self._use_uuid = xmlroot.get(XMIResource.xmiid) is not None
        self._contents = [modelroot]
        self._later = []
        self._resolve_mem = {}
        for key, value in xmlroot.attrib.items():
            namespace, att_name = XMIResource.extract_namespace(key)
            prefix = self.reverse_nsmap[namespace] if namespace else None
            if prefix == 'xmi' and att_name == 'id':
                modelroot._xmiid = value
                self.uuid_dict[value] = modelroot
            elif namespace:
                try:
                    # Do stuff with this
                    # metaclass = self.get_metamodel(namespace)
                    pass
                except KeyError:
                    pass
            elif not namespace:
                feature = self._find_in_metacache(eobject, key)
                if not feature:
                    continue
                modelroot.__setattr__(key, value)
        return modelroot

    def _decode_eobject(self, current_node, parent_eobj):
        eobject_info = self._decode_node(parent_eobj, current_node)
        feat_container, eobject, eatts, erefs = eobject_info
        if not feat_container:
            return

        # deal with eattributes and ereferences
        for eattribute, value in eatts:
            if eattribute.many:
                for strval in value.split():
                    val = eattribute.eType.from_string(strval)
                    eobject.__getattribute__(eattribute.name).append(val)
            val = eattribute.eType.from_string(value)
            eobject.__setattr__(eattribute.name, val)

        if erefs:
            self._later.append((eobject, erefs))

        # attach the new eobject to the parent one
        if feat_container and feat_container.many:
            parent_eobj.__getattribute__(feat_container.name).append(eobject)
        elif feat_container:
            parent_eobj.__setattr__(feat_container.name, eobject)

        # iterate on children
        for child in current_node:
            self._decode_eobject(child, eobject)

    def _decode_node(self, parent_eobj, node):
        if node.tag == 'eGenericType':  # Special case, TODO
            return (None, None, [], [])
        feature_container = self._find_in_metacache(parent_eobj, node.tag)
        if not feature_container:
            raise ValueError('Feature {0} is unknown for {1}, line {2}'
                             .format(node.tag,
                                     parent_eobj.eClass.name,
                                     node.sourceline,))
        if node.get('href'):
            ref = node.get('href')
            decoder = self._get_href_decoder(ref)
            return (feature_container, decoder.resolve(ref), [], [])
        if self._type_attribute(node):
            prefix, _type = node.get(XMIResource.xsitype).split(':')
            if not prefix:
                raise ValueError('Prefix {0} is not registered, line {1}'
                                 .format(prefix, node.tag))
            epackage = self.prefix2epackage(prefix)
            etype = epackage.getEClassifier(_type)
            if not etype:
                raise ValueError('Type {0} is unknown in {1}, line{2}'
                                 .format(_type, epackage, node.tag))
        else:
            etype = feature_container.eType

        # we create the instance
        if etype is Ecore.EClass:
            name = node.get('name')
            eobject = etype(name)
        elif etype is Ecore.EStringToStringMapEntry \
                and feature_container is Ecore.EAnnotation.details:
            annotation_key = node.get('key')
            annotation_value = node.get('value')
            parent_eobj.details[annotation_key] = annotation_value
            if annotation_key == 'documentation':
                parent_eobj.eContainer().__doc__ = annotation_value
            return (None, None, [], [])
        else:
            eobject = etype()
        eobject._eresource = self

        # we sort the node feature (no containments)
        eatts = []
        erefs = []
        for key, value in node.attrib.items():
            feature = self._decode_attribute(eobject, key, value)
            if not feature:
                continue  # we skipp the unknown feature
            if etype is Ecore.EClass and feature.name == 'name':
                continue  # we skip the name for metamodel import
            if isinstance(feature, Ecore.EAttribute):
                eatts.append((feature, value))
            else:
                erefs.append((feature, value))
        return (feature_container, eobject, eatts, erefs)

    def _decode_attribute(self, owner, key, value):
        namespace, att_name = XMIResource.extract_namespace(key)
        prefix = self.reverse_nsmap[namespace] if namespace else None
        # This is a special case, we are working with uuids
        if prefix == 'xmi' and att_name == 'id':
            owner._xmiid = value
            self.uuid_dict[value] = owner
        elif prefix == 'xsi' and att_name == 'type':
            # type has already been handled
            pass
        elif namespace:
            pass
        elif not namespace:
            if att_name == 'href':
                return
            feature = self._find_in_metacache(owner, att_name)
            if not feature:
                raise ValueError('Feature {0} does not exists for type {1}'
                                 .format(att_name, owner.eClass.name))
            return feature

    def _decode_ereferences(self):
        opposite = []
        for eobject, erefs in self._later:
            for ref, value in erefs:
                if ref.name == 'eOpposite':
                    opposite.append((eobject, ref, value))
                    continue
                if ref.many:
                    values = map(Resource.normalize, value.split())
                else:
                    values = [value]
                for value in values:
                    # decoder = self._get_decoder(value)
                    # resolved_value = decoder.resolve(value)
                    resolved_value = self._resolve_nonhref(value)
                    if not resolved_value:
                        raise ValueError('EObject for {0} is unknown'
                                         .format(value))
                    if ref.many:
                        eobject.__getattribute__(ref.name) \
                               .append(resolved_value)
                    else:
                        eobject.__setattr__(ref.name, resolved_value)

        for eobject, ref, value in opposite:
            # decoder = self._get_decoder(value)
            # resolved_value = decoder.resolve(value)
            resolved_value = self._resolve_nonhref(value)
            if not resolved_value:
                raise ValueError('EObject for {0} is unknown'.format(value))
            eobject.__setattr__(ref.name, resolved_value)

    def _resolve_nonhref(self, path):
        uri, fragment = self._is_external(path)
        if fragment in self._resolve_mem:
            return self._resolve_mem[fragment]
        if uri:
            epackage = self.get_metamodel(uri)
            if not epackage:
                raise TypeError('Cannot resolve metamodel: {0}'.format(uri))
            val = Resource._navigate_from(fragment, epackage)
            self._resolve_mem[uri] = val
            return val
        return self.resolve(fragment)

    def _clean_registers(self):
        delattr(self, '_later')
        delattr(self, '_meta_cache')

    def register_nsmap(self, prefix, uri):
        if uri in self.reverse_nsmap:
            return
        if prefix not in self.prefixes:
            self.prefixes[prefix] = uri
            self.reverse_nsmap[uri] = prefix
            return
        l = filter(lambda x: x.startswith(prefix), self.prefixes.keys())
        prefix = '{0}_{1}'.format(prefix, len(list(l)))
        self.prefixes[prefix] = uri
        self.reverse_nsmap[uri] = prefix

    def register_eobject_epackage(self, eobj):
        epackage = eobj.eClass.ePackage
        prefix = epackage.nsPrefix
        nsURI = epackage.nsURI
        self.register_nsmap(prefix, nsURI)

    def save(self, output=None):
        output = output.create_outstream() \
                 if output \
                 else self.uri.create_outstream()
        self.prefixes = {}
        self.reverse_nsmap = {}
        # Compute required nsmap for subpackages
        if not self.contents:
            tree = etree.ElementTree()
        else:
            root = self.contents[0]
            self.register_eobject_epackage(root)
            for eobj in root.eAllContents():
                self.register_eobject_epackage(eobj)

            tree = etree.ElementTree(self._go_across(root))
        with output as out:
            tree.write(out,
                       pretty_print=True,
                       xml_declaration=True,
                       encoding=tree.docinfo.encoding)

    def _go_across(self, obj):
        eclass = obj.eClass
        if not obj.eContainmentFeature():  # obj is the root
            epackage = eclass.ePackage
            prefix = epackage.nsPrefix
            nsURI = epackage.nsURI
            tag = etree.QName(nsURI, eclass.name) if nsURI else eclass.name
            nsmap = {xmi: xmi_url,
                     xsi: xsi_url}
            nsmap.update(self.prefixes)
            node = etree.Element(tag, nsmap=nsmap)
            xmi_version = etree.QName(xmi_url, 'version')
            node.attrib[xmi_version] = '2.0'
        else:
            node = etree.Element(obj.eContainmentFeature().name)
            if obj.eContainmentFeature().eType != eclass:
                xsi_type = etree.QName(xsi_url, 'type')
                uri = eclass.ePackage.nsURI
                prefix = self.reverse_nsmap[uri]
                node.attrib[xsi_type] = '{0}:{1}' \
                                        .format(prefix, eclass.name)
        if self._use_uuid:
            self._assign_uuid(obj)
            xmi_id = '{{{0}}}id'.format(xmi_url)
            node.attrib[xmi_id] = obj._xmiid

        for feat in obj._isset:
            if feat.derived:
                continue
            value = obj.__getattribute__(feat.name)
            if hasattr(feat.eType, 'eType') and feat.eType.eType is dict:
                tag = feat.name
                for key, val in value.items():
                    entry = etree.Element(tag)
                    entry.attrib['key'] = key
                    entry.attrib['value'] = val
                    node.append(entry)
            elif isinstance(feat, Ecore.EAttribute):
                etype = feat.eType
                if feat.many and value:
                    node.attrib[feat.name] = ' '.join(etype.to_string(value))
                elif value != feat.get_default_value():
                    node.attrib[feat.name] = etype.to_string(value)

            elif isinstance(feat, Ecore.EReference) and \
                    feat.eOpposite and feat.eOpposite.containment:
                continue
            elif isinstance(feat, Ecore.EReference) \
                    and not feat.containment:
                if feat.many and value:
                    results = list(map(self._build_path_from, value))
                    embedded = []
                    crossref = []
                    for frag, cref in results:
                        if cref:
                            crossref.append(frag)
                        else:
                            embedded.append(frag)
                    if embedded:
                        result = ' '.join(embedded)
                        node.attrib[feat.name] = result
                    for ref in crossref:
                        sub = etree.SubElement(node, feat.name)
                        sub.attrib['href'] = ref
                elif not feat.many and value:
                    # node.attrib[feat.name] = self._build_path_from(value)
                    frag, cref = self._build_path_from(value)
                    if cref:
                        sub = etree.SubElement(node, feat.name)
                        sub.attrib['href'] = frag
                    else:
                        node.attrib[feat.name] = frag

            if isinstance(feat, Ecore.EReference) and feat.containment:
                children = obj.__getattribute__(feat.name)
                children = children if feat.many else [children]
                for child in children:
                    node.append(self._go_across(child))
        return node

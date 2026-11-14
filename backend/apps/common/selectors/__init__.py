from apps.companies.models import CompanyMembership


def get_active_company_id(request):
    """
    Extracts active company scope from query params (`?company=`) or header (`X-Company-ID`).
    """
    if request is None:
        return None
    company_id = request.query_params.get("company") or request.headers.get("X-Company-ID")
    return company_id or None


def scope_queryset_by_company(queryset, request, company_lookup="company_id"):
    """
    Applies multi-company scoping to querysets per DB Design §7 Rule #5:
    'Implement company scoping in queryset/service layers and apply it to list,
    retrieve, update, delete and export endpoints.'
    """
    if request is None:
        return queryset

    active_company_id = get_active_company_id(request)
    if active_company_id:
        queryset = queryset.filter(**{company_lookup: active_company_id})

    user = getattr(request, "user", None)
    if user and user.is_authenticated and not user.is_superuser:
        authorized_company_ids = CompanyMembership.objects.filter(
            user=user,
            status=CompanyMembership.Status.ACTIVE,
        ).values_list("company_id", flat=True)
        queryset = queryset.filter(**{f"{company_lookup}__in": authorized_company_ids})

    return queryset

